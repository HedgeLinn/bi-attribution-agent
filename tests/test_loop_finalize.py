"""harness/loop.py `_finalize` 的验收:结论结构化收尾 + 有界重试(M4)。

契约:`harness/loop.py::_finalize` 的 docstring(签名与行为描述即为契约)。

设计约束:**不 import 真 LLM、不需要 BI_API_KEY**。`_finalize` 与模型的全部交互只有
`llm.invoke(messages)` 一个协议,所以一个假 llm(记录调用次数 + 收到的消息快照)就足以把
「首次可解析 / 重试后成功 / 两次都失败」三条路径钉死。

刻意覆盖的边界:空内容、非 dict 的 JSON(数组/标量)、前后带说明文字的 JSON 子串、
围栏包裹的 JSON、两种 token 用量字段形状,以及重试消息在 messages 中的位置。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from harness import loop

# 可解析的「标准答案」结论(键名与 loop 的契约无关,只要求是 JSON object)
VALID = '{"conclusion": "hypothesis confirmed", "confidence": "high"}'

# 不可解析的典型形态:模型用自然语言讲了一通,没有 JSON
PROSE = "先说结论:整体是自然回落,但某一家表现明显异常。"


class FakeLLM:
    """假 LLM:按脚本依次吐出响应,并记录每次 invoke 收到的消息快照。

    响应耗尽后再被调用 -> 断言失败。用来把「不该重试时一次都没调」「该重试时恰好调一次」
    这类次数约束变成硬断言,而不是靠肉眼观察。
    """

    def __init__(self, *responses: AIMessage) -> None:
        self._responses = list(responses)
        self.calls: list[list] = []

    @property
    def invoke_count(self) -> int:
        return len(self.calls)

    def invoke(self, messages):
        # 存快照:调用方在 invoke 之后还会继续改 messages,不拷贝就观察不到当时的样子
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("假 LLM 被调用的次数超出了脚本给定的响应数")
        return self._responses.pop(0)


def _reply(content: str, inp: int = 0, out: int = 0, style: str = "none") -> AIMessage:
    """构造一条带 token 用量的假响应(字段形状取自真实 ChatOpenAI 响应)。"""
    if style == "usage_metadata":
        return AIMessage(content=content, usage_metadata={
            "input_tokens": inp, "output_tokens": out, "total_tokens": inp + out})
    if style == "response_metadata":
        return AIMessage(content=content, response_metadata={
            "token_usage": {"prompt_tokens": inp, "completion_tokens": out}})
    return AIMessage(content=content)


def _history(*tail) -> list:
    """一段典型的会话历史:系统提示 + 用户问题 + 若干已发生的消息。"""
    return [SystemMessage(content="sys"), HumanMessage(content="为什么下滑?"), *tail]


# --- 1 / 2. 首次内容可解析:原样返回,绝不惊动模型 ---

def test_parseable_content_returned_as_is_without_any_invoke():
    llm = FakeLLM()  # 无任何响应:一旦被调用即断言失败
    messages = _history()
    before = list(messages)

    content, in_tokens, out_tokens = loop._finalize(llm, messages, VALID, 11, 22, None)

    assert content == VALID                      # 原样返回,不重新序列化
    assert (in_tokens, out_tokens) == (11, 22)   # token 不变
    assert llm.invoke_count == 0                 # 不触发重试
    assert messages == before                    # 不改动历史


@pytest.mark.parametrize("fenced", [
    "```json\n" + VALID + "\n```",
    "```\n" + VALID + "\n```",
])
def test_fenced_json_is_parseable_and_returned_verbatim(fenced):
    llm = FakeLLM()

    content, in_tokens, out_tokens = loop._finalize(llm, _history(), fenced, 3, 4, None)

    assert content == fenced                     # 返回原文(围栏不剥掉,前端已兼容)
    assert (in_tokens, out_tokens) == (3, 4)
    assert llm.invoke_count == 0


def test_prefix_prose_with_json_substring_is_parseable():
    """模型爱写「分析如下:{...} 以上」——子串兜底应判为可解析,不触发重试。"""
    llm = FakeLLM()
    content = "分析如下:" + VALID + " 以上。"

    out_content, _, _ = loop._finalize(llm, _history(), content, 1, 1, None)

    assert out_content == content
    assert llm.invoke_count == 0


# --- 3. 不可解析 -> 重试;重试成功则用重试内容 + 累计 token ---

@pytest.mark.parametrize("style", ["usage_metadata", "response_metadata"])
def test_retry_success_returns_retry_content_and_accumulated_tokens(style):
    llm = FakeLLM(_reply(VALID, inp=40, out=9, style=style))
    messages = _history(AIMessage(content=PROSE))

    content, in_tokens, out_tokens = loop._finalize(llm, messages, PROSE, 100, 7, None)

    assert content == VALID                      # 用重试内容,而不是原文
    assert (in_tokens, out_tokens) == (140, 16)  # 首轮 + 重试都要计入
    assert llm.invoke_count == 1                 # 有界:只重试一次


def test_retry_success_with_fenced_json():
    llm = FakeLLM(_reply("```json\n" + VALID + "\n```", inp=2, out=1,
                         style="usage_metadata"))

    content, in_tokens, out_tokens = loop._finalize(llm, _history(), PROSE, 5, 5, None)

    assert content == "```json\n" + VALID + "\n```"
    assert (in_tokens, out_tokens) == (7, 6)


def test_retry_response_without_usage_info_degrades_to_unchanged_tokens():
    """真实网关偶尔不回 usage:token 保持原值,不得抛错、不得算成负数。"""
    llm = FakeLLM(_reply(VALID))

    _, in_tokens, out_tokens = loop._finalize(llm, _history(), PROSE, 8, 2, None)

    assert (in_tokens, out_tokens) == (8, 2)


# --- 4. 两次都不可解析 -> 诚实降级:返回原文,token 仍然计入 ---

def test_both_unparseable_returns_original_content_honestly():
    llm = FakeLLM(_reply("还是不行,我再补充两点。", inp=5, out=2, style="usage_metadata"))
    messages = _history(AIMessage(content=PROSE))

    content, in_tokens, out_tokens = loop._finalize(llm, messages, PROSE, 10, 1, None)

    assert content == PROSE                      # 原文,不编造、不截断、不返回空
    assert (in_tokens, out_tokens) == (15, 3)    # 白花的重试 token 也要记账
    assert llm.invoke_count == 1                 # 重试次数上界 = _FINALIZE_RETRIES


def test_retry_budget_follows_module_constant(monkeypatch):
    """重试次数必须由 _FINALIZE_RETRIES 决定,不是写死的 1。

    把常量调大再观察调用次数:写死 `range(1)` 的实现在这里会露出马脚。
    """
    assert loop._FINALIZE_RETRIES >= 1
    monkeypatch.setattr(loop, "_FINALIZE_RETRIES", 3)
    llm = FakeLLM(*[_reply("仍然不是 JSON") for _ in range(3)])

    content, _, _ = loop._finalize(llm, _history(), PROSE, 0, 0, None)

    assert llm.invoke_count == 3                 # 跟着常量走,而不是跟着 1 走
    assert content == PROSE


def test_later_retry_can_still_succeed(monkeypatch):
    """常量调大后:最后一次重试成功,仍应返回成功那一次的内容。"""
    monkeypatch.setattr(loop, "_FINALIZE_RETRIES", 3)
    llm = FakeLLM(_reply("还是不是 JSON"), _reply("仍然不是 JSON"),
                  _reply(VALID, inp=2, out=1, style="usage_metadata"))

    content, in_tokens, out_tokens = loop._finalize(llm, _history(), PROSE, 1, 1, None)

    assert content == VALID
    assert llm.invoke_count == 3
    assert (in_tokens, out_tokens) == (3, 2)     # 前两次没回 usage,按 (0,0) 计


def test_empty_content_is_unparseable_and_triggers_retry():
    """空串:不可解析 -> 重试;重试仍为空 -> 返回空串(而不是 None)。"""
    llm = FakeLLM(_reply(""))

    content, _, _ = loop._finalize(llm, _history(), "", 0, 0, None)

    assert content == ""
    assert isinstance(content, str)
    assert llm.invoke_count == 1


# --- 5. _parse_conclusion 的边界(判决「是否可解析」的唯一入口) ---

@pytest.mark.parametrize("content", [
    None,               # 无内容(模型回了空 tool_calls + 空 content)
    "",                 # 空串
    "   \n  ",          # 只有空白
    PROSE,              # 纯自然语言
    "[1, 2, 3]",        # JSON 数组(非 dict)
    '[{"a": 1}]',       # 数组里套对象:顶层仍是数组
    "123",              # JSON 标量
    "true",
    '"a string"',
    "{不是合法的 JSON",  # 括号对不上
    "{'single': 'quotes'}",  # Python 字面量不是 JSON,不能算「可解析」
])
def test_parse_conclusion_rejects_non_object_input(content):
    assert loop._parse_conclusion(content) is None


@pytest.mark.parametrize("content", [
    VALID,
    "```json\n" + VALID + "\n```",
    "前言" + VALID + "后记",
])
def test_parse_conclusion_accepts_json_object(content):
    assert isinstance(loop._parse_conclusion(content), dict)


def test_valid_json_that_is_not_object_never_falls_back_to_inner_object():
    """顶层是合法 JSON 但不是 object -> 判 None;不会退到 `{`..`}` 子串去捞里面的对象。

    容易被误读成「数组里也有对象,应该算可解析」——实际 `json.loads` 成功即返回,
    类型不是 dict 就是 None。评分侧据此口径记「结论不可解析」。
    """
    assert loop._parse_conclusion('["a", {"b": 2}]') is None


# --- 6. 重试消息:追加在末位,且内容是「直接给 JSON」的指令 ---

def test_retry_message_is_appended_at_tail_of_history():
    llm = FakeLLM(_reply(VALID, inp=1, out=1, style="usage_metadata"))
    history = _history(AIMessage(content=PROSE))
    before = list(history)

    loop._finalize(llm, history, PROSE, 0, 0, None)

    sent = llm.calls[0]
    assert len(sent) == len(before) + 1          # 只多一条消息,不动已有历史
    assert sent[:-1] == before
    assert isinstance(sent[-1], HumanMessage)    # 末位是人类指令,模型才知道要重发
    assert "JSON" in sent[-1].content
    assert "代码块" in sent[-1].content           # 明确禁止 markdown 围栏
    assert isinstance(sent[-1].content, str)


def test_history_untouched_when_no_retry_needed():
    """可解析时不追加任何消息(否则会在真实 loop 里污染多轮历史)。"""
    llm = FakeLLM()
    history = _history(AIMessage(content=PROSE))
    before = list(history)

    loop._finalize(llm, history, VALID, 0, 0, None)

    assert history == before
    assert llm.calls == []


# --- 接线验收:run() 的两处收尾确实走了 _finalize(不改 run,只观察) ---

def _patch_run(monkeypatch, llm, tool_objects=()) -> None:
    """把 run() 的外部依赖全换成假件:模型、工具集、语义层、提示词渲染。"""
    monkeypatch.setattr(loop, "build_llm", lambda tools=None: llm)
    monkeypatch.setattr(loop.tools, "build_tools", lambda: list(tool_objects))
    monkeypatch.setattr(loop.tools, "semantic", lambda: None)
    monkeypatch.setattr(loop.tools, "dataset_context", lambda: {})
    monkeypatch.setattr(loop.context, "render_system_prompt", lambda *a, **k: "sys")


def test_run_wires_finalize_into_both_exit_paths(monkeypatch):
    """run() 的两处收尾都必须经过 _finalize:正常结束 + 轮数用尽后的强制总结。"""
    # 路径一:模型直接给结论但不可解析 -> 重试一次后才返回
    llm = FakeLLM(_reply(PROSE, inp=10, out=4, style="usage_metadata"),
                  _reply(VALID, inp=3, out=2, style="usage_metadata"))
    _patch_run(monkeypatch, llm)
    events: list[dict] = []

    assert loop.run("为什么下滑?", verbose=False, on_event=events.append) == VALID
    assert llm.invoke_count == 2                              # 首次 + 一次重试
    assert events[-1] == {"type": "final", "content": VALID}  # 前端拿到的是校验后的内容
    usage = next(e for e in events if e["type"] == "usage")
    assert (usage["input_tokens"], usage["output_tokens"]) == (13, 6)

    # 路径二:一直调工具直到轮数用尽 -> 强制总结仍不可解析 -> 重试
    monkeypatch.setattr(loop, "MAX_ITERATIONS", 1)
    llm2 = FakeLLM(AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}]),
                   _reply(PROSE), _reply(VALID, inp=1, out=1, style="usage_metadata"))
    _patch_run(monkeypatch, llm2, [SimpleNamespace(name="t", invoke=lambda args: {"n": 1})])

    assert loop.run("为什么下滑?", verbose=False) == VALID
    assert llm2.invoke_count == 3                             # 工具轮 + 强制总结 + 重试
    assert isinstance(llm2.calls[-1][-1], HumanMessage)       # 重试指令落在强制总结之后
    assert "JSON" in llm2.calls[-1][-1].content
