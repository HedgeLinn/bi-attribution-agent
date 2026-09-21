# -*- coding: utf-8 -*-
"""图表数据层与渲染计划的纯函数测试(零 streamlit、零 altair,毫秒级)。

钉两类事:

1. **「没有数值不等于 0」** —— nan / ±inf / 字符串 / None 都要被剔除或降级,不许补 0。
2. **控件键与折叠计划** —— 键撞车在回放时会直接抛 DuplicateWidgetID;折叠要保留**最近**的。
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:          # app/ 内模块用平铺名互相引用
    sys.path.insert(0, str(APP_DIR))

from chart_data import (bands_from_anomaly, bars_from_contribute_rate, keep_events,  # noqa: E402
                        rows_from_query)
from chart_plan import (auto_choice, expand_key, expand_state_key,  # noqa: E402
                        looks_like_time, plan_tasks, switcher_key)


def test_rows_from_query_labels_follow_dims_order() -> None:
    """标签按 dims 的顺序取行上的字段值,多字段用 / 连接。"""
    result = {"rows": [{"region": "华东", "city": "上海", "value": 3.0},
                       {"region": "华南", "city": "深圳", "value": 1.0}]}
    assert rows_from_query(result, ["region", "city"]) == [
        {"label": "华东 / 上海", "value": 3.0}, {"label": "华南 / 深圳", "value": 1.0}]
    assert rows_from_query(result, ["city"]) == [
        {"label": "上海", "value": 3.0}, {"label": "深圳", "value": 1.0}]


def test_rows_from_query_drops_non_finite_values() -> None:
    """拿不到有限数的行**整行剔除**,不拿 0 补位。"""
    result = {"rows": [{"k": "a", "value": float("nan")}, {"k": "b", "value": float("inf")},
                       {"k": "c", "value": "12"}, {"k": "d", "value": None},
                       {"k": "e", "value": True}, {"k": "f", "value": 7.5}]}
    assert rows_from_query(result, ["k"]) == [{"label": "f", "value": 7.5}]


def test_rows_from_query_degrades_on_garbage() -> None:
    """非 dict / 缺 rows / rows 不是列表 / 行不是 dict -> 空列表(不抛)。"""
    for bad in (None, "x", 3, {}, {"rows": None}, {"rows": "x"}, {"rows": [None, 3]}):
        assert rows_from_query(bad, ["k"]) == []


def test_bars_from_contribute_rate_keeps_finite_rates() -> None:
    """比率型切片 -> 变化率条形;缺 change_rate 或非有限的切片剔除。"""
    result = {"top": [{"key": "华东", "label": "华东", "change_rate": -0.1586},
                      {"key": "华中", "label": "华中", "change_rate": -0.0229},
                      {"key": "西南", "label": "西南"},
                      {"key": "西北", "label": "西北", "change_rate": float("nan")}]}
    assert bars_from_contribute_rate(result) == [
        {"label": "华东", "rate": -0.1586}, {"label": "华中", "rate": -0.0229}]


def test_bars_from_contribute_rate_empty_when_nothing_usable() -> None:
    """一个有限变化率都收不到 -> 空(调用方据此不画,而不是画一张空图)。"""
    for bad in (None, {}, {"top": None}, {"top": "x"}, {"top": [{"key": "a"}]}):
        assert bars_from_contribute_rate(bad) == []


def test_bands_from_anomaly_uses_mad() -> None:
    """基线 ±MAD 成带;对比窗口没有带宽(lo == hi)。"""
    got = bands_from_anomaly({"base": 100.0, "cmp": 80.0, "baseline_mad": 10.0})
    assert got == [{"kind": "统计基线", "value": 100.0, "lo": 90.0, "hi": 110.0},
                   {"kind": "对比窗口", "value": 80.0, "lo": 80.0, "hi": 80.0}]


def test_bands_from_anomaly_none_without_baseline() -> None:
    """基线不可估计(base 为 None,对应 baseline_type="none")-> None:不拿 0 冒充基线;
    MAD 缺失或为 0 时不编造带宽。"""
    assert bands_from_anomaly({"base": None, "cmp": 80.0, "baseline_mad": 10.0}) is None
    assert bands_from_anomaly({"base": 100.0, "cmp": None}) is None
    assert bands_from_anomaly({}) is None
    zero = bands_from_anomaly({"base": 100.0, "cmp": 80.0, "baseline_mad": 0})
    assert zero[0]["lo"] == zero[0]["hi"] == 100.0


def test_keep_events_filters_usage_and_final() -> None:
    """只留 tool_call / tool_result:usage 与 token 字段重复、final 与 content 重复。"""
    events = [{"type": "tool_call", "step": 1, "name": "contribute", "args": {}},
              {"type": "usage", "input_tokens": 1, "cost_cny": 0.1},
              {"type": "tool_result", "step": 1, "name": "contribute", "result": {}},
              {"type": "final", "content": "{}"}, None, "x", 3]
    assert [e["type"] for e in keep_events(events)] == ["tool_call", "tool_result"]
    assert keep_events(None) == [] and keep_events("x") == []


def test_plan_tasks_keeps_most_recent() -> None:
    """超限时保留**最近**的若干张(用户刚问的最相关);展开则全部可见。"""
    tasks = list(range(10))
    assert plan_tasks(tasks, expanded=False, limit=4) == ([6, 7, 8, 9], 6)
    assert plan_tasks(tasks, expanded=True, limit=4) == (tasks, 0)
    assert plan_tasks(tasks[:3], expanded=False, limit=4) == ([0, 1, 2], 0)
    assert plan_tasks([], expanded=False, limit=4) == ([], 0)


def test_plan_tasks_boundaries() -> None:
    """上限 0 时**一张都不画**(不能因为 `items[-0:]` 是全部而被整批放行);
    总数恰好等于上限时不折叠。"""
    tasks = list(range(10))
    assert plan_tasks(tasks, expanded=False, limit=0) == ([], 10)
    assert plan_tasks(tasks, expanded=False, limit=10) == (tasks, 0)
    assert plan_tasks(tasks, expanded=False, limit=100) == (tasks, 0)


def test_widget_keys_carry_scope() -> None:
    """键必须带 scope:回放时多条消息连着渲染,撞 key 会抛 DuplicateWidgetID。"""
    assert switcher_key("m1", 0) != switcher_key("m2", 0)
    assert switcher_key("m1", 0) != switcher_key("m1", 1)
    keys = {expand_key("m1"), expand_key("m2"), expand_state_key("m1"),
            expand_state_key("m2"), switcher_key("m1", 0)}
    assert len(keys) == 5


def test_auto_choice_by_data_shape() -> None:
    """时间序列默认折线、分类默认柱状;少于两个点退回表格(画不出趋势)。"""
    assert looks_like_time([{"label": "2026-01-01"}, {"label": "2026-01-02"}]) is True
    assert looks_like_time([{"label": "华东"}, {"label": "华南"}]) is False
    assert looks_like_time([]) is False
    assert auto_choice([{"label": "2026-01-01"}, {"label": "2026-01-02"}]) == "折线"
    assert auto_choice([{"label": "华东"}, {"label": "华南"}]) == "柱状"
    assert auto_choice([{"label": "华东"}]) == "表格"