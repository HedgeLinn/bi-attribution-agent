# -*- coding: utf-8 -*-
"""回放链路与交互的端到端测试(AppTest 真跑 streamlit)。

有三件事 stub 测不了,只能真跑:

1. **回放** —— 消息里存的**事件流**能重建成图。刷新页面后图还在,靠的就是这条路径。
2. **切换器** —— 点选真的换了图型(`segmented_control` 的返回值只有真控件才有,stub 只会
   返回 None)。
3. **分级渲染** —— 超限时确实少画了图、展开按钮能把它们放回来。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from chart_data import keep_events                                  # noqa: E402
from tests.viz_fixtures import call, cont, real                      # noqa: E402,F401


def _vega(at) -> list:
    return [*at.get("vega_lite_chart"), *at.get("arrow_vega_lite_chart")]


def _marks(spec_text: str) -> list[str]:
    """取一张图的 mark 类型;分层图(如折线=面积+线、基线对比=柱+带)取各层。"""
    spec = json.loads(spec_text)
    if "layer" in spec:
        return [layer["mark"]["type"] for layer in spec["layer"]]
    return [spec["mark"]["type"]]


def _replay_app(messages, expanded=False) -> None:
    """回放宿主:与 app.py 的回放分支同形(层级序与图型提示都从消息里取)。

    源码被原样当脚本执行 —— import 必须在函数体内。
    """
    import streamlit as st

    from attribution_viz import render_from_events
    from chart_plan import expand_state_key
    from history import decode_events

    for index, msg in enumerate(messages):
        scope = msg.get("id") or f"m{index}"
        parsed = msg.get("parsed")
        render_from_events(st, decode_events(msg), msg.get("level_orders"), scope=scope,
                           expanded=st.session_state.get(expand_state_key(scope), False),
                           chart_hint=parsed.get("图表") if isinstance(parsed, dict) else None)


def _chart_msg(events: list, msg_id: str = "m1") -> dict:
    return {"id": msg_id, "events": json.dumps(keep_events(events), ensure_ascii=False)}


def test_replay_rebuilds_charts_from_stored_events(real) -> None:
    """回放:消息里的事件流 -> 树 + 各图(与当轮走同一段渲染代码)。"""
    msg = _chart_msg([*call(0, "detect_anomaly", {"metric": "gmv"}, real["anomaly"]),
                      *cont(1, "gmv", "region", real["region"])])
    at = AppTest.from_function(_replay_app, default_timeout=30,
                               kwargs={"messages": [msg]}).run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.get("graphviz_chart")) == 1
    charts = _vega(at)
    assert len(charts) == 2                       # 基线对比 + 区域贡献瀑布
    assert "bar" in _marks(charts[0].spec) and "bar" in _marks(charts[1].spec)


def test_replay_tolerates_legacy_messages(real) -> None:
    """老消息没有 events 键 / 脏数据 -> 零图零异常(向后兼容,不做数据迁移)。"""
    legacy = [{"id": "m1", "content": "{}", "parsed": None},
              {"id": "m2", "events": "{不是 JSON"},
              {"id": "m3", "events": "[null, 3, {\"type\": \"usage\"}]"}]
    at = AppTest.from_function(_replay_app, default_timeout=30,
                               kwargs={"messages": legacy}).run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.get("graphviz_chart") and not _vega(at)


def test_switcher_changes_chart_type(real) -> None:
    """切换器真的换图:默认柱状 -> 折线 -> 表格(切换只换渲染,不重取数)。"""
    result = {"metric": "gmv", "rows": [{"region": "华东", "value": 3.0},
                                        {"region": "华南", "value": 1.0}]}
    events = call(0, "query_metric", {"metric": "gmv", "dims": ["region"]}, result)
    at = AppTest.from_function(_replay_app, default_timeout=30,
                               kwargs={"messages": [_chart_msg(events)]}).run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.segmented_control) == 1
    assert _marks(_vega(at)[0].spec) == ["bar"]           # 分类数据默认柱状

    at.segmented_control[0].set_value("折线").run()
    assert "line" in _marks(_vega(at)[0].spec)

    at.segmented_control[0].set_value("表格").run()
    assert len(at.dataframe) == 1 and not _vega(at)       # 表格走 st.dataframe


def test_replay_honours_stored_chart_hint(real) -> None:
    """回放要用消息里存的图型提示 —— 否则同一条消息刷新后默认图型会变。

    这批数据是分类的(自动判定为柱状),提示指定折线,所以画出来必须是折线。
    """
    result = {"metric": "gmv", "rows": [{"region": "华东", "value": 3.0},
                                        {"region": "华南", "value": 1.0}]}
    events = call(0, "query_metric", {"metric": "gmv", "dims": ["region"]}, result)
    msg = {"id": "m1", "parsed": {"图表": "折线"},
           "events": json.dumps(keep_events(events), ensure_ascii=False)}
    at = AppTest.from_function(_replay_app, default_timeout=30,
                               kwargs={"messages": [msg]}).run()
    assert not at.exception, [e.value for e in at.exception]
    assert "line" in _marks(_vega(at)[0].spec)

    without = dict(msg, parsed=None)                 # 没有提示 -> 回到数据形态判定
    at2 = AppTest.from_function(_replay_app, default_timeout=30,
                                kwargs={"messages": [without]}).run()
    assert _marks(_vega(at2)[0].spec) == ["bar"]


def test_switcher_keys_do_not_collide_across_messages(real) -> None:
    """两条消息各带一张查询图 -> 两个切换器互不撞 key(撞了会抛 DuplicateWidgetID)。"""
    result = {"metric": "gmv", "rows": [{"region": "华东", "value": 3.0},
                                        {"region": "华南", "value": 1.0}]}
    events = call(0, "query_metric", {"metric": "gmv", "dims": ["region"]}, result)
    messages = [_chart_msg(events, "m1"), _chart_msg(events, "m2")]
    at = AppTest.from_function(_replay_app, default_timeout=30,
                               kwargs={"messages": messages}).run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.segmented_control) == 2


def test_graded_rendering_folds_older_charts(real, monkeypatch) -> None:
    """超限时只画**最近**的若干张 + 一个展开按钮;点它之后全部画回来。"""
    import chart_plan

    monkeypatch.setattr(chart_plan, "CHART_LIMIT", 2)
    events: list = []
    for step in range(5):
        events += cont(step, "gmv", "region", real["region"])
    at = AppTest.from_function(_replay_app, default_timeout=30,
                               kwargs={"messages": [_chart_msg(events)]}).run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(_vega(at)) == 2                    # 只画最近两步
    assert len(at.button) == 1                    # 「显示更早的图」

    at.button[0].click().run()                    # 回调置 session_state 标志位
    assert not at.exception, [e.value for e in at.exception]
    assert len(_vega(at)) == 5                    # 展开后全出
    assert not at.button