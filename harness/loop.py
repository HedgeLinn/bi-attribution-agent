"""harness 的 Agent Loop 组件:手写的「假设-验证」循环。

这是整个 harness 最核心、也最该被理解的部分。它不依赖 LangGraph 等编排框架,
只靠 LangChain 的 bind_tools + 一个 while 循环,把 LLM 从「一次问答」
变成「可调用工具、多轮决策、直到给出结论」的智能体。

核心结构(经典 agent loop):
    感知 -> 决策 -> 行动 -> 观察 -> 回到感知 ...
直到模型发出 finish(不再返回 tool_call),循环结束。

对归因 agent 而言,这个 loop 的每一步「行动」就是一次维度下钻,
「观察」就是下钻返回的贡献度排序 —— 模型据此决定下一个假设。

本模块**不含任何数据集知识**:系统提示词由 harness.context 渲染
(数据集无关的方法论 + 由语义层渲染的数据集上下文,§3.4),
工具 schema 由 harness.tools 按语义层现场生成。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from attribution.annotations import Annotation, append_annotation
from harness import context, tools
from harness.datasets import DEFAULT_DATASETS_DIRNAME
from harness.llm import build_llm, MODEL

# 单次运行 token 成本展示:复用 llm_cost 的单价表(与全局记账口径一致)。
# llm_cost 是本机私有技能,其它机器 clone 后没有则退化为固定 0,不影响主流程。
try:
    sys.path.insert(0, "C:/Users/hzl/.claude/skills/llm_cost")
    from llm_cost import cost_of  # noqa: E402
except Exception:  # noqa: BLE001
    def cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
        """llm_cost 不可用时的成本估算(返回 0,仅保证流程不断)。"""
        return 0.0

MAX_ITERATIONS = 20        # 最多下钻轮数,防止死循环(模型持续调用工具时强制终止)
_FINALIZE_RETRIES = 1      # 结论 JSON 不可解析时的重试次数(有界:不追着模型无限要格式;
                           # 重试不把上次响应追加回历史,>1 时后几次输入相同,意义有限)


def _extract_usage(msg: AIMessage) -> tuple[int, int]:
    """从 AIMessage 里抠出本次调用的 (input_tokens, output_tokens)。

    优先读 usage_metadata;没有则退回 response_metadata 里的 token_usage。
    读不到就 (0, 0),不影响主流程。
    """
    um = getattr(msg, "usage_metadata", None)
    if isinstance(um, dict) and um:
        inp = um.get("input_tokens") or um.get("prompt_tokens") or 0
        out = um.get("output_tokens") or um.get("completion_tokens") or 0
        if inp or out:
            return int(inp), int(out)
    rm = getattr(msg, "response_metadata", None) or {}
    tu = rm.get("token_usage") or rm.get("usage")
    if isinstance(tu, dict):
        inp = tu.get("prompt_tokens") or tu.get("input_tokens") or 0
        out = tu.get("completion_tokens") or tu.get("output_tokens") or 0
        return int(inp), int(out)
    return 0, 0


def run(query: str, verbose: bool = True, on_event=None, persist: bool = True) -> str:
    """跑一轮完整的归因分析。query 是用户的问题,返回模型最终的分析结论。

    on_event: 可选回调,签名 on_event(event: dict)。事件类型:
      - {"type": "tool_call", "step": i, "name": ..., "args": {...}}
      - {"type": "tool_result", "step": i, "name": ..., "result": {...}}
      - {"type": "usage", "input_tokens": ..., "output_tokens": ..., "cost_cny": ...}
      - {"type": "final", "content": "..."}
    供前端实时展示「假设-验证」过程用;传 None 则只靠 verbose print。
    persist: 结束时把结构化结论沉淀到 annotations.jsonl(§6.3);评估等测量场景
    传 False,别让批量跑分把 repo 内的沉淀文件写成自己的答案。
    """
    # 每次运行都按当前语义层重建:工具 schema 与系统提示词都跟着语义层走
    tool_objects = tools.build_tools()
    runnable = {tool.name: tool for tool in tool_objects}
    llm = build_llm(tools=tool_objects)
    system_prompt = context.render_system_prompt(tools.semantic(), tools.dataset_context(), query)
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=query)]

    in_tokens = out_tokens = 0

    for i in range(MAX_ITERATIONS):
        resp = llm.invoke(messages)
        messages.append(resp)  # 把 assistant 消息(含 tool_call)加入历史
        in_tokens, out_tokens = _accumulate(in_tokens, out_tokens, resp)

        if not resp.tool_calls:
            # 没有工具调用 = 模型决定结束,给出最终结论;收尾时校验并做有界重试(M4)
            content, in_tokens, out_tokens = _finalize(
                llm, messages, resp.content, in_tokens, out_tokens, on_event)
            _emit_finish(on_event, content, in_tokens, out_tokens)
            _persist_conclusion(query, content, persist, verbose)
            return content

        # 有工具调用:逐个执行,把观察结果作为 tool 消息追加
        _run_tool_calls(resp, runnable, messages, i + 1, verbose, on_event)

    # 达到最大轮数仍未结束,强制让模型总结
    messages.append(HumanMessage(content="已达到最大下钻轮数,请基于已有证据直接给出最终 JSON 结论。"))
    final_resp = llm.invoke(messages)
    in_tokens, out_tokens = _accumulate(in_tokens, out_tokens, final_resp)
    content, in_tokens, out_tokens = _finalize(
        llm, messages, final_resp.content, in_tokens, out_tokens, on_event)
    _emit_finish(on_event, content, in_tokens, out_tokens)
    _persist_conclusion(query, content, persist, verbose)
    return content


def _accumulate(in_tokens: int, out_tokens: int, msg: AIMessage) -> tuple[int, int]:
    """把本次调用的 token 用量累加到总量上。"""
    tin, tout = _extract_usage(msg)
    return in_tokens + tin, out_tokens + tout


def _strip_fences(content: str) -> str:
    """去掉模型可能包在 JSON 外的 ``` 围栏。"""
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _parse_conclusion(content: str) -> dict | None:
    """把最终结论字符串解析成 dict;失败返回 None。

    先整串解析,再找首个 { 到最后一个 } 的子串重试(模型可能带前置说明文字)。
    """
    if not content:
        return None
    for candidate in (_strip_fences(content), content):
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            pass
    text = _strip_fences(content)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _finalize(llm, messages: list, content: str, in_tokens: int, out_tokens: int,
              on_event) -> tuple[str, int, int]:
    """收尾(M4):校验最终结论是可解析的 JSON,不可解析则要求模型重发一次(有界)。

    契约:
      - 首次内容可解析(含围栏剥离)-> 原样返回(content, in, out)不变
      - 不可解析 -> 追加一条 HumanMessage(「直接输出 JSON,不要代码块、不要说明文字」)
        再 invoke 一次;第二次仍不可解析 -> 返回原文(诚实降级,评分侧会记「结论不可解析」)
      - 重试 invoke 抛异常(网关抖动/超时)-> 同样返回首次原文:结论宁缺毋崩,
        不把已经拿到的内容连同异常一起丢掉
      - 重试消耗的 token 计入返回值;on_event 的 usage/final 事件由调用方统一发
      - 返回类型始终是 str 内容(前端 parse_final 已兼容 JSON 与非 JSON)
    """
    if _parse_conclusion(content) is not None:
        return content, in_tokens, out_tokens

    # 不可解析:明确要求重发一次。只追加一条人类消息(不动已有历史),
    # 重试次数由 _FINALIZE_RETRIES 约束,不追着模型无限要格式。
    messages.append(HumanMessage(
        content="请直接输出 JSON 格式的结论(不要 markdown 代码块、不要说明文字)。"))

    for _ in range(_FINALIZE_RETRIES):
        try:
            resp = llm.invoke(messages)
        except Exception:  # noqa: BLE001  网关抖动:返回首次原文,不把结论丢掉
            return content, in_tokens, out_tokens
        in_tokens, out_tokens = _accumulate(in_tokens, out_tokens, resp)
        if _parse_conclusion(resp.content) is not None:
            return resp.content, in_tokens, out_tokens

    # 重试机会用完仍不可解析:原样返回首次内容(诚实降级——评分侧据此记
    # 「结论不可解析」,而不是替模型编一个结论出来)。
    return content, in_tokens, out_tokens


def _emit_finish(on_event, content: str, in_tokens: int, out_tokens: int) -> None:
    """把累计用量与最终结论推给前端(on_event 为 None 时什么都不做)。"""
    if not on_event:
        return
    on_event({"type": "usage", "input_tokens": in_tokens, "output_tokens": out_tokens,
              "cost_cny": round(cost_of(MODEL, in_tokens, out_tokens), 4)})
    on_event({"type": "final", "content": content})


def _run_tool_calls(resp: AIMessage, runnable: dict, messages: list, step: int,
                    verbose: bool, on_event) -> None:
    """执行本轮的全部 tool_call,把观察结果作为 tool 消息追加进历史。

    工具抛异常也要把错误反馈给模型(而不是中断整轮分析),让它自己修正参数。
    """
    for call in resp.tool_calls:
        tool_name, args = call["name"], call["args"]
        if verbose:
            print(f"\n[{step}] 调用工具 {tool_name}{json.dumps(args, ensure_ascii=False)}")
        if on_event:
            on_event({"type": "tool_call", "step": step, "name": tool_name, "args": args})
        try:
            result = runnable[tool_name].invoke(args)
        except Exception as e:  # noqa: BLE001  工具执行失败也要反馈给模型,让它自己修正
            result = {"error": str(e)}
        if verbose:
            print(f"      -> {json.dumps(result, ensure_ascii=False)[:400]}")
        if on_event:
            on_event({"type": "tool_result", "step": step, "name": tool_name, "result": result})
        messages.append(ToolMessage(content=json.dumps(result, ensure_ascii=False),
                                    tool_call_id=call["id"]))


# ---------------------------------------------------------------------------
# 结论沉淀(§6.3 的写半侧):把最终 JSON 结论转成一条 annotation 追加进数据集目录
# ---------------------------------------------------------------------------
# 「图表」只是给前端挑默认图型的提示,不是结论内容 —— 留在 confirmed 里会挤占
# 回注提示词的 240 字符预算(annotations 会按相关性把 confirmed 摘要注回上下文)
_EXCLUDED_FROM_CONFIRMED = ("已排除", "证据链", "图表")


def _to_annotation(query: str, content: str, ts: str) -> Annotation | None:
    """结构化结论 -> 沉淀记录;结论 JSON 不可解析时返回 None(不沉淀半截结论)。

    「已排除」-> ruled_out、「证据链」-> evidence,并从 confirmed 里弹出:
    这两个键不参与相关性检索(annotations._searchable 只收 query/hypotheses/
    confirmed),留在 confirmed 里会让被否掉的假设与长证据链污染后续回注。
    """
    conclusion = _parse_conclusion(content)
    if conclusion is None:
        return None
    return Annotation(
        ts=ts,
        query=query,
        confirmed={key: value for key, value in conclusion.items()
                   if key not in _EXCLUDED_FROM_CONFIRMED},
        ruled_out=_text_items(conclusion.get("已排除")),
        evidence=_text_items(conclusion.get("证据链")),
    )


def _text_items(value) -> tuple[str, ...]:
    """结论里的文本列表键:列表逐项转文本,裸字符串包成单元素(模型手写 JSON 常漏方括号)。"""
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _persist_conclusion(query: str, content: str, persist: bool, verbose: bool) -> None:
    """把本轮结论沉淀到 datasets/<id>/annotations.jsonl(§6.3)。

    沉淀是锦上添花:结论不可解析、语义层不可用、写入失败都静默放弃,不许拖垮
    主流程;verbose 时打印放弃原因,好让操作者知道沉淀没有发生。
    """
    if not persist:
        return
    try:
        annotation = _to_annotation(
            query, content, datetime.now().astimezone().isoformat(timespec="seconds"))
        if annotation is None:
            if verbose:
                print("[沉淀] 结论不是 JSON,本次分析不沉淀")
            return
        target = Path(DEFAULT_DATASETS_DIRNAME) / tools.semantic().dataset / "annotations.jsonl"
        append_annotation(str(target), annotation)
    except Exception as err:  # noqa: BLE001  沉淀失败不影响本轮分析结果
        if verbose:
            print(f"[沉淀] 归因结论未写入:{err}")
