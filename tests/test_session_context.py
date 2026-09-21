# -*- coding: utf-8 -*-
"""会话内多轮对话记忆的纯函数测试(零 streamlit,毫秒级)。

钉两件事:
1. summarize_turn 对各类工具结果的摘要渲染正确、降级安全
2. build_context_hint 对消息列表的提取逻辑正确
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from session_context import build_context_hint, summarize_turn  # noqa: E402


# ---------------------------------------------------------------------------
# summarize_turn: 各工具类型的渲染
# ---------------------------------------------------------------------------

def _events(*items):
    """快捷构造: (tool, args, result) 三元组 -> events 列表。"""
    out = []
    for i, (tool, args, result) in enumerate(items):
        out.append({"type": "tool_call", "step": i, "name": tool, "args": args})
        out.append({"type": "tool_result", "step": i, "name": tool, "result": result})
    return out


def test_summarize_full_turn() -> None:
    """完整归因链: anomaly + 两次 contribute + query + decompose + parsed 结论。"""
    events = _events(
        ("detect_anomaly", {"metric": "gmv"},
         {"metric": "gmv", "change_rate": -0.123, "base": 1000000, "cmp": 877000,
          "is_anomaly": True}),
        ("contribute", {"metric": "gmv", "dimension": "region", "level": "region"},
         {"metric": "gmv", "dimension": "region", "top": [
             {"key": "华东", "label": "华东", "contribution": 0.78}]}),
        ("contribute", {"metric": "gmv", "dimension": "city", "level": "city",
                        "filters": {"region": "华东"}},
         {"metric": "gmv", "dimension": "city", "top": [
             {"key": "上海", "label": "上海", "contribution": 0.82}]}),
        ("query_metric", {"metric": "客单价", "dims": ["region"]},
         {"metric": "客单价", "rows": [{"region": "华东", "value": 150},
                                      {"region": "华南", "value": 200}]}),
        ("decompose", {"target": "gmv"},
         {"target": "gmv", "kind": "multiplicative", "factors": ["量", "价"],
          "effects": [{"factor": "量", "effect": 0.052},
                      {"factor": "价", "effect": -0.404}]}),
    )
    parsed = {"结论": "上海徐家汇旗舰店头部 SKU 下架导致客单价暴跌",
              "已排除": ["618 大促回落"]}
    text = summarize_turn(parsed, events)

    assert "[上轮分析]" in text
    assert "异常: gmv" in text and "-12.3%" in text and "异常" in text
    assert "下钻: region(华东" in text and "78%" in text
    assert "下钻: city(上海" in text and "82%" in text
    assert "验证: 客单价" in text and "2行" in text
    assert "分解: gmv" in text and "量" in text and "价" in text
    assert "结论: 上海徐家汇" in text
    assert "已排除: 618 大促回落" in text


def test_summarize_skips_tool_errors() -> None:
    """工具报错(result 是字符串)→ 跳过该步,不崩溃。"""
    events = _events(
        ("detect_anomaly", {"metric": "gmv"},
         {"metric": "gmv", "change_rate": -0.1, "is_anomaly": True, "base": 100, "cmp": 90}),
        ("contribute", {"metric": "gmv", "dimension": "region", "level": "region"},
         "工具执行超时"),  # 报错:字符串
    )
    text = summarize_turn(None, events)
    assert "异常:" in text
    assert "下钻:" not in text        # 报错的 contribute 不渲染


def test_summarize_no_events_with_parsed() -> None:
    """没有 events 但有 parsed 结论 → 只有结论行(仍生成摘要)。"""
    parsed = {"结论": "数据质量问题:某段时间取值为空", "已排除": []}
    text = summarize_turn(parsed, [])
    assert "结论: 数据质量问题" in text
    assert "已排除" not in text       # 空列表不渲染


def test_summarize_none_parsed() -> None:
    """parsed 为 None → 不渲染结论行,但 events 正常渲染。"""
    events = _events(
        ("detect_anomaly", {"metric": "gmv"},
         {"metric": "gmv", "change_rate": 0.02, "is_anomaly": False,
          "base": 100, "cmp": 102}),
    )
    text = summarize_turn(None, events)
    assert "异常:" in text
    assert "未见异常" in text
    assert "结论:" not in text


def test_summarize_empty_events_no_parsed() -> None:
    """events 空 + parsed None → 空字符串(不生成只有标题的摘要)。"""
    assert summarize_turn(None, []) == ""
    assert summarize_turn({}, []) == ""


def test_summarize_contribute_ratio_type() -> None:
    """比率型 contribute(无 contribution,有 change_rate)→ 用环比渲染。"""
    events = _events(
        ("contribute", {"metric": "客单价", "dimension": "region", "level": "region"},
         {"metric": "客单价", "dimension": "region", "top": [
             {"key": "华东", "label": "华东", "change_rate": -0.352}]}),
    )
    text = summarize_turn(None, events)
    assert "下钻: region(华东" in text and "-35.2%" in text


def test_summarize_truncation() -> None:
    """超长摘要硬截断到 600 字符 + '…'。"""
    # 造很多 contribute 步骤
    items = []
    for i in range(50):
        items.append(("contribute", {"metric": "gmv", "dimension": f"dim_{i}",
                                     "level": f"lv_{i}"},
                       {"metric": "gmv", "dimension": f"dim_{i}",
                        "top": [{"key": f"k{i}", "label": f"切片{i}号很长的名字用来撑长度",
                                 "contribution": 0.5}]}))
    events = _events(*items)
    text = summarize_turn(None, events)
    assert len(text) <= 601        # 600 + "…"
    assert text.endswith("…")


def test_summarize_decompose_no_effects() -> None:
    """decompose 无 effects → 跳过该行。"""
    events = _events(
        ("decompose", {"target": "gmv"},
         {"target": "gmv", "kind": "additive", "effects": []}),
    )
    text = summarize_turn(None, events)
    assert "分解:" not in text


# ---------------------------------------------------------------------------
# build_context_hint: 从消息列表提取
# ---------------------------------------------------------------------------

def test_build_context_hint_empty_messages() -> None:
    """空消息列表 → None。"""
    assert build_context_hint([]) is None
    assert build_context_hint(None) is None


def test_build_context_hint_no_events() -> None:
    """老消息没有 events → None。"""
    messages = [
        {"role": "user", "content": "为什么 GMV 下滑？"},
        {"role": "assistant", "content": "{}", "parsed": {"结论": "某原因"}},
    ]
    assert build_context_hint(messages) is None


def test_build_context_hint_picks_latest() -> None:
    """多条 assistant 消息 → 取最近一条有 events 的。"""
    import json
    old_events = _events(
        ("detect_anomaly", {"metric": "gmv"},
         {"metric": "gmv", "change_rate": -0.05, "is_anomaly": True, "base": 100, "cmp": 95}),
    )
    new_events = _events(
        ("detect_anomaly", {"metric": "mrr"},
         {"metric": "mrr", "change_rate": -0.20, "is_anomaly": True, "base": 500, "cmp": 400}),
    )
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "{}", "parsed": {"结论": "旧结论"},
         "events": json.dumps(old_events, ensure_ascii=False)},
        {"role": "user", "content": "Q2"},
        {"role": "assistant", "content": "{}", "parsed": {"结论": "新结论"},
         "events": json.dumps(new_events, ensure_ascii=False)},
    ]
    hint = build_context_hint(messages)
    assert hint is not None
    assert "mrr" in hint           # 最新一条的指标
    assert "gmv" not in hint       # 不是旧的
    assert "新结论" in hint


def test_build_context_hint_skips_no_events_assistant() -> None:
    """最近的 assistant 消息没有 events,跳过找更早的。"""
    import json
    old_events = _events(
        ("detect_anomaly", {"metric": "gmv"},
         {"metric": "gmv", "change_rate": -0.05, "is_anomaly": True, "base": 100, "cmp": 95}),
    )
    messages = [
        {"role": "assistant", "content": "{}", "parsed": {"结论": "旧结论"},
         "events": json.dumps(old_events, ensure_ascii=False)},
        {"role": "user", "content": "Q2"},
        {"role": "assistant", "content": "纯文本回复", "parsed": None},  # 无 events
    ]
    # 最近一条 assistant 没有 events → 跳过 → 找上一条有 events 的
    hint = build_context_hint(messages)
    assert hint is not None
    assert "gmv" in hint


def test_build_context_hint_first_turn() -> None:
    """首轮只有用户消息,没有 assistant → None。"""
    messages = [{"role": "user", "content": "为什么 GMV 下滑？"}]
    assert build_context_hint(messages) is None
