# -*- coding: utf-8 -*-
"""会话内多轮对话记忆:从上轮 events 提取结构化摘要,供下次 run() 注入提示词。

问题: run() 是无状态函数,每次调用都从零构建 messages,模型看不到上一轮对话的
分析过程。前端存了 events + parsed 结论,但只服务于 UI 回放。

解法: 本模块从最近一条 assistant 消息的 events 里提取下钻链(复用
attribution_viz.extract_drilldown),渲染成一段紧凑摘要,作为 context_hint 传给
run()。评估场景不传 context_hint = 原行为,零侵入。
"""

from __future__ import annotations

from typing import Any

from chart_data import num                          # 安全的有限数提取
from history import decode_events

# 复用 attribution_viz 的下钻链提取(纯数据函数,零 streamlit 依赖)
from attribution_viz import extract_drilldown

_HINT_MAX = 600          # 摘要硬截断:防挤占当前分析的空间

__all__ = ["build_context_hint", "summarize_turn"]


def _fmt_rate(rate) -> str:
    """变化率 -> '+12.3%' / '-5.0%' / ''(非有限数)。"""
    v = num(rate)
    return "" if v is None else f"{v:+.1%}"


def _fmt_num(v) -> str:
    """数值 -> 紧凑字符串;None / 非有限返回 ''。"""
    n = num(v)
    return "" if n is None else f"{n:g}"


def _line_anomaly(result: dict) -> str | None:
    """detect_anomaly 结果 -> 一行摘要。"""
    if not isinstance(result, dict):
        return None
    rate = _fmt_rate(result.get("change_rate"))
    base = _fmt_num(result.get("base"))
    cmp = _fmt_num(result.get("cmp"))
    flag = "异常" if result.get("is_anomaly") else "未见异常"
    parts = [result.get("metric", "?")]
    if rate:
        parts.append(f"环比{rate}")
    if base and cmp:
        parts.append(f"基线={base}, 对比={cmp}")
    parts.append(f"({flag})")
    return "异常: " + " ".join(parts)


def _line_contribute(result: dict) -> str | None:
    """contribute 结果 -> 一行摘要(只取 top-1 切片,不铺开全部)。"""
    if not isinstance(result, dict):
        return None
    top = result.get("top")
    if not isinstance(top, list) or not top:
        return None
    best = top[0]
    if not isinstance(best, dict):
        return None
    dim = result.get("dimension", "?")
    label = best.get("label", "?")
    contrib = num(best.get("contribution"))
    rate = _fmt_rate(best.get("change_rate"))
    suffix = f"贡献{contrib:.0%}" if contrib is not None else (f"环比{rate}" if rate else "")
    return f"下钻: {dim}({label} {suffix})" if suffix else f"下钻: {dim}({label})"


def _line_query(result: dict, args: dict) -> str | None:
    """query_metric 结果 -> 一行摘要(不展开数据)。"""
    if not isinstance(result, dict):
        return None
    rows = result.get("rows")
    n = len(rows) if isinstance(rows, list) else 0
    dims = ", ".join(args.get("dims", [])) or "(无分组)"
    return f"验证: {result.get('metric', '?')} by [{dims}] ({n}行)"


def _line_decompose(result: dict) -> str | None:
    """decompose 结果 -> 一行摘要(每个 factor 的 effect)。"""
    if not isinstance(result, dict):
        return None
    effects = result.get("effects")
    if not isinstance(effects, list) or not effects:
        return None
    target = result.get("target", "?")
    kind = result.get("kind", "?")
    parts = []
    for eff in effects:
        if not isinstance(eff, dict):
            continue
        factor = eff.get("factor", "?")
        effect = _fmt_rate(eff.get("effect")) or _fmt_num(eff.get("effect"))
        parts.append(f"{factor}{effect}" if effect else factor)
    return f"分解: {target}({kind}) → {', '.join(parts)}" if parts else None


def summarize_turn(parsed, events: list[dict]) -> str:
    """从一轮分析的 parsed 结论 + events 生成对话摘要。

    返回格式:
        [上轮分析]
        异常: GMV 环比-12.3% 基线=100万, 对比=87.7万(异常)
        下钻: region(华东 贡献78%)
        下钻: city(上海 贡献82%)
        验证: 客单价 by [region, city] (3行)
        分解: GMV(multiplicative) → 量+5.2%, 价-40.4%
        结论: 上海徐家汇旗舰店头部 SKU 下架导致客单价暴跌
        已排除: 618 大促回落

    总长度硬截断 _HINT_MAX 字符。
    """
    lines = ["[上轮分析]"]
    drilldown = extract_drilldown(events)

    for item in drilldown:
        tool = item.get("tool")
        result = item.get("result")
        args = item.get("args") or {}
        # 工具报错时 result 是字符串,跳过该步
        if not isinstance(result, dict):
            continue
        line = None
        if tool == "detect_anomaly":
            line = _line_anomaly(result)
        elif tool == "contribute":
            line = _line_contribute(result)
        elif tool == "query_metric":
            line = _line_query(result, args)
        elif tool == "decompose":
            line = _line_decompose(result)
        if line:
            lines.append(line)

    # 结论与已排除从 parsed 取(比 events 更精炼)
    if isinstance(parsed, dict):
        conclusion = parsed.get("结论")
        if isinstance(conclusion, str) and conclusion:
            lines.append(f"结论: {conclusion}")
        ruled_out = parsed.get("已排除")
        if isinstance(ruled_out, list) and ruled_out:
            items = [s for s in ruled_out if isinstance(s, str) and s]
            if items:
                lines.append(f"已排除: {'; '.join(items)}")

    text = "\n".join(lines)
    # 只有标题行(没有任何有效内容)-> 不生成摘要
    if len(lines) <= 1:
        return ""
    return text if len(text) <= _HINT_MAX else text[:_HINT_MAX] + "…"


def build_context_hint(messages: list[dict]) -> str | None:
    """从消息列表提取上轮分析摘要。

    倒序找最近一条有 events 的 assistant 消息;找不到(首轮/老消息无 events)→ None。
    返回 None 时 context.py 不注入任何内容。
    """
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        events = decode_events(msg)
        if not events:
            continue
        parsed = msg.get("parsed")
        hint = summarize_turn(parsed, events)
        return hint if hint else None
    return None
