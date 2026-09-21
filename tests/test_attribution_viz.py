"""app/attribution_viz.py 的离线测试:纯数据函数、渲染降级、AppTest 端到端冒烟。

只钉三类:① 恒等式 Σdelta ≡ total_change;② 降级——报错 / 未知工具 / 空流 / nan 不许抛错,
失败必须标出、不许解释成「比率型不可加」;③ 几何——y0/y1 自己算,不交给 Vega 的 stack。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "app"
# app/ 内模块之间用平铺名互相引用(streamlit 运行时就是这个路径);这里的 insert 让
# pytest 也能按同样方式导入 —— 与 tests/test_import_wizard.py 一致的模式。
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from attribution_viz import (build_tree, extract_drilldown,               # noqa: E402
                             render_from_events, render_tree)
from chart_attr import render_waterfall                                   # noqa: E402
from chart_data import (slice_label, waterfall_from_contribute,           # noqa: E402
                        waterfall_from_decompose, waterfall_rows)
from tests.viz_fixtures import Stub, call, cont, events, real  # noqa: E402,F401

_TRUNC = "其余切片(已省略 2 条之外的切片)"     # 括号里是**已进图**的条数(省略几条不可知)
_NO_SLICE, _UNREADABLE = "无切片明细(仅总量)", "结果不可读"


def _exact(value):                                  # 金额量级下 rel 太松:按绝对容差比
    return pytest.approx(value, abs=1e-6, rel=0)




def test_extract_drilldown_orders_pairs_and_degrades() -> None:
    """按 step 升序取出结果、args 与调用配对、字符串原样保留;脏输入不抛错。"""
    items = extract_drilldown([*call(2, "unknown_tool", {"x": 1}, "工具报错了"),
                              *call(0, "detect_anomaly", {"metric": "m"}, {"is_anomaly": True}),
                              *call(1, "contribute", {"dimension": "d", "level": "l"},
                                     {"top": []})])
    assert [it["step"] for it in items] == [0, 1, 2]
    assert [it["tool"] for it in items] == ["detect_anomaly", "contribute", "unknown_tool"]
    assert items[1]["args"] == {"dimension": "d", "level": "l"}
    assert items[2]["result"] == "工具报错了"          # 字符串原样搬,不当 dict 用
    assert extract_drilldown([None, "x", {"type": "final"}]) == [] == extract_drilldown(None)
    assert extract_drilldown([{"type": "tool_result", "step": 0, "name": "contribute"}]) == [
        {"step": 0, "tool": "contribute", "args": {}, "result": None}]
    again = extract_drilldown(call(0, "c", {"a": 1}, "r1") + call(0, "c", {"a": 2}, "r2"))
    assert [it["args"] for it in again] == [{"a": 1}, {"a": 2}]     # 同一步的多次调用按序配对
    mixed = extract_drilldown(call("x", "c", {}, "s") + call(0, "c", {}, "n"))
    assert [it["step"] for it in mixed] == [0, "x"]                 # 非整数 step 排末尾


def test_build_tree_draws_root_dimension_slices_and_drill_chain(real) -> None:
    """异常检测 = 根;contribute = 维度节点(标签带指标名)+ 切片;filters 命中父切片就挂该
    切片,否则挂上一个维度节点(默认只按「level 名称不同」弱判定,见下条)。"""
    region, city, hit = real["region"], real["city"], real["region"]["top"][0]["key"]
    tree = build_tree(extract_drilldown(
        call(0, "detect_anomaly", {"metric": "gmv"}, real["anomaly"])
        + cont(1, "gmv", "region", region)
        + cont(2, "gmv", "city", city, filters={"region": hit})
        + cont(3, "gmv", "store_id", real["store"])))
    nodes, edges, first = tree["nodes"], tree["edges"], 2 + len(region["top"])
    second = first + 1 + len(city["top"])          # 第二次下钻的维度节点
    assert nodes[0]["tool"] == "detect_anomaly" and (0, 1) in edges
    assert nodes[0]["label"].startswith(str(real["anomaly"]["metric"]))  # 根:来自检测结果
    assert nodes[1]["label"] == "gmv·store/region"                      # F5:标签带指标名
    assert all((1, i) in edges for i in range(2, first))               # 切片挂维度节点
    assert all(n["detail"] == "store/region" for n in nodes[2:first])
    slice_index = next(i for i, n in enumerate(nodes) if n["label"].startswith(str(hit)))
    assert nodes[first]["label"] == "gmv·store/city" and (slice_index, first) in edges
    assert (first, second) in edges and (second, second + 1) in edges    # 无 filters -> 挂上层


def test_build_tree_chain_metric_rules_and_parallel_anomalies(real) -> None:
    """F2/F4 反例:换指标 / 层级回退都挂根(回退只有传 level_orders 才判得出,默认弱判定
    认不出);第二次 detect_anomaly 另起一棵并列的树,其后的下钻挂它下面、不嵌进上一棵。"""
    orders = {"store": ("region", "city", "store_id")}
    res = {"metric": "m", "total_base": 10.0, "total_cmp": 8.0, "total_change": -2.0,
           "top": [{"key": "s1", "base": 5.0, "change": -1.0}]}
    rollback = extract_drilldown(cont(1, "m", "region", res) + cont(2, "m", "city", res)
                                 + cont(3, "m", "region", res))
    deep = build_tree(rollback, orders)
    assert (0, 2) in deep["edges"]                             # region -> city:index 0 -> 1
    assert not any(child == 4 for _, child in deep["edges"])    # 回退(region)不成链 -> 挂根
    assert (2, 4) in build_tree(rollback)["edges"]              # 不传 level_orders:认不出回退
    cross = extract_drilldown(cont(1, "m", "city", res) + cont(2, "n", "region", res))
    assert not any(c == 2 for _, c in build_tree(cross, orders)["edges"])  # 换指标 != 同一条链
    skipped = extract_drilldown(cont(1, "m", "region", res) + cont(2, "m", "store_id", res))
    assert (0, 2) in build_tree(skipped, orders)["edges"]        # 隔层下钻也算加深:0 -> 2
    assert not any(c == 2 for _, c in build_tree(                # 层级不在序里 -> index -1
        skipped, {"store": ("region", "city")})["edges"])
    items = extract_drilldown(                                   # F4:第二次检测另起一棵树
        call(0, "detect_anomaly", {"metric": "gmv"}, real["anomaly"])
        + cont(1, "gmv", "region", real["region"])
        + call(2, "detect_anomaly", {"metric": "aov"},
                {"metric": "aov", "is_anomaly": True, "change_rate": -0.1})
        + cont(3, "aov", "region", real["region"]))
    edges, second = build_tree(items)["edges"], 2 + len(real["region"]["top"])
    assert (0, 1) in edges and (second, second + 1) in edges      # 各自的贡献挂各自的根
    assert not any(child == second for _, child in edges)         # 第二个异常不嵌在第一个下
    assert all(not (a < second <= b) for a, b in edges)           # 两棵分支互不嵌套


def test_build_tree_evidence_and_degradation(real) -> None:
    """query_metric 挂最后一个维度节点;报错(字符串形态)照画但标 ⚠️;空输入给空树。"""
    region = real["region"]
    items = extract_drilldown(
        call(0, "detect_anomaly", {"metric": "gmv"}, real["anomaly"])
        + cont(1, "gmv", "region", region)
        + call(2, "query_metric", {"metric": "gmv", "dims": ["region"]}, {"rows": [1]})
        + call(3, "contribute", {"dimension": "store", "level": "region"}, "工具报错了")
        + call(4, "who_knows", {}, {"whatever": 1}))
    nodes, edges = build_tree(items)["nodes"], build_tree(items)["edges"]
    q, err = 2 + len(region["top"]), 3 + len(region["top"])
    assert nodes[q]["label"].startswith("查询") and (1, q) in edges      # 证据挂维度节点
    assert nodes[err]["label"].endswith("⚠️") and nodes[err]["detail"] == "工具报错了"  # 原文兜底
    assert (0, err) in edges and not any(a == err for a, _ in edges)     # 同级重复 -> 挂根
    who = nodes[err + 1]["label"]
    assert who.startswith("who_knows") and "whatever" in who and (0, err + 1) in edges
    assert build_tree([]) == {"nodes": [], "edges": []} == build_tree(None)
    assert build_tree(["x", 7, None]) == {"nodes": [], "edges": []}


def test_waterfall_from_contribute_identity_labels_and_degrades(real) -> None:
    """首末条 = 总量、Σdelta ≡ total_change;截断补残差条;比率型不可加就不画;切片三态
    (无明细 / 不可读 / 混进非 dict)各说各的,账都平;nan / inf 不进标签(F6)。"""
    result = real["region"]
    bars = waterfall_from_contribute(result)
    assert [bars[0]["base"], bars[-1]["base"]] == [result["total_base"], result["total_cmp"]]
    assert bars[0]["delta"] == bars[-1]["delta"] == 0.0
    assert sum(bar["delta"] for bar in bars) == _exact(result["total_change"])
    truncated = {"metric": "m", "total_base": 100.0, "total_cmp": 70.0, "total_change": -30.0,
                 "top": [{"key": "a", "base": 40.0, "change": -10.0},
                         {"key": "b", "base": 30.0, "change": -5.0}]}
    bars = waterfall_from_contribute(truncated)
    assert [bar["label"] for bar in bars] == ["m 基期", "a -10", "b -5", _TRUNC, "m 对比期"]
    assert sum(bar["delta"] for bar in bars) == _exact(-30.0) == bars[-2]["delta"] + -15.0
    derived = {"metric": "aov", "total_base": 5.0, "total_cmp": 4.0, "total_change": -1.0,
               "top": [{"key": "华东", "base": 5.0, "change_rate": -0.2}]}
    assert waterfall_from_contribute(derived) == [] == waterfall_from_contribute(None)
    base = {"total_base": 3.0, "total_cmp": 1.0}
    for top, label in (([], _NO_SLICE), ("x", _UNREADABLE),
                       ([{"key": "a", "change": 1.0}, 7], _UNREADABLE)):
        bars = waterfall_from_contribute({**base, "top": top})
        assert bars[-2]["label"] == label            # 残差条在末条之前:三种说法各不同
        assert sum(bar["delta"] for bar in bars) == _exact(-2.0)
    nan, inf = float("nan"), float("inf")
    assert slice_label({"key": "x", "change": nan}) == "x"   # F6:数值非有限就只留切片名
    assert slice_label({"key": "x", "change": inf, "change_rate": -0.5}) == "x -50.0%"
    assert slice_label({"key": "x", "change_rate": nan}) == "x"
    assert slice_label({"key": "x", "change": -3.0}) == "x -3" and slice_label({}) == "?"


def test_waterfall_from_decompose_transports_effects_as_is(real) -> None:
    """整窗分解:首末条是总量、label/effect 原样搬运;近似标注不洗掉、不凑 total_change。"""
    result = real["decompose"]
    numeric = [e for e in result["effects"] if isinstance(e.get("effect"), (int, float))]
    bars = waterfall_from_decompose(result)
    assert len(bars) == len(numeric) + 2 and bars[0]["base"] == result["total_base"]
    assert [bar["label"] for bar in bars[1:-1]] == [e["label"] for e in numeric]
    assert [bar["delta"] for bar in bars[1:-1]] == [e["effect"] for e in numeric]
    assert bars[-1]["base"] == result["total_cmp"]
    approximated = {"target": "t", "total_base": 10.0, "total_cmp": 6.0, "effects": [
        {"factor": "f1", "label": "结构效应(含非正值,效应为近似)", "base": None, "effect": -4.5},
        {"factor": "f2", "label": "自身效应", "base": 2.0, "effect": 1.0}],
        "slices": [{"key": "s1", "effects": []}]}
    bars = waterfall_from_decompose(approximated)
    assert len(bars) == 4 and bars[1]["label"] == "结构效应(含非正值,效应为近似)"
    assert bars[1]["base"] is None and sum(b["delta"] for b in bars) == _exact(-3.5)
    assert waterfall_from_decompose(None) == [] == waterfall_from_decompose(
        {"effects": [], "total_base": 1.0, "total_cmp": 2.0})
    assert waterfall_from_decompose({"effects": [{"effect": 1.0}], "total_base": 1.0}) == []


def test_render_skips_empty_and_emits_explicit_interval_chart(real) -> None:
    """空树 / 空 bars 不渲染;有数据时交出合法 DOT(转义、丢弃畸形边)与 altair 图;
    F7 的区间(y0/y1)在 Python 里算好、跌破 0 也对:图上不许再交给 Vega 的 stack。"""
    stub = Stub()
    for empty in ({"nodes": [], "edges": []}, {}, None):   # 空树不画
        render_tree(stub, empty)
    for empty in ({}, None):                               # 空 bars 不画
        render_waterfall(stub, empty)
    assert stub.calls == []
    tree = {"nodes": [{"step": 0, "tool": "detect_anomaly", "label": '带"引号"的标签'},
                      {"step": 1, "tool": "who_knows", "label": None}],
            "edges": [(0, 1), (0, 9), ("x", 1), (1,)]}
    render_tree(stub, tree)
    _, dot, _ = stub.calls[0]
    assert dot.startswith("digraph") and dot.rstrip().endswith("}") and "who_knows" in dot
    assert '\\"引号\\"' in dot and dot.count("-> ") == 1   # 引号转义;畸形边丢弃
    bars = [{"label": "m 基期", "base": 100.0, "delta": 0.0},
            {"label": "a", "base": 40.0, "delta": -60.0},
            {"label": "b", "base": 30.0, "delta": -50.0},
            {"label": "m 对比期", "base": -10.0, "delta": 0.0}]
    assert [(r["y0"], r["y1"], r["kind"]) for r in waterfall_rows(bars)] == [
        (0.0, 100.0, "flat"), (40.0, 100.0, "down"), (-10.0, 40.0, "down"), (-10.0, 0.0, "flat")]
    render_waterfall(stub, bars, title="T")
    _, chart, kwargs = stub.calls[1]
    spec = chart.to_dict()
    assert "stack" not in (spec["encoding"]["y"] or {})    # 不给 Vega 拆栈的机会
    assert spec["encoding"]["y2"]["field"] == "y1" and spec["mark"] == {"type": "bar"}
    assert len(spec["data"]["values"]) == len(bars) and kwargs["width"] == "stretch"
    # ↑ 每条一行,不拆段;width="stretch" 替代已被 streamlit 1.63 弃用的 use_container_width



def test_render_from_events_dispatches_error_ratio_and_charts(real, events) -> None:
    """总入口:F1 工具失败(dict)标 ⚠️ + 一行说明、原样带出错误文本、绝不说成「比率型」;
    可加结果 -> 一张树 + 每个结果一张瀑布图;比率型 -> 只说明;空流 -> 什么都不画。"""
    error, msg = real["error"], real["error"]["error"]
    error_events = (call(0, "detect_anomaly", {"metric": "gmv"}, real["anomaly"])
                    + cont(1, "gmv", "store_name", error))
    node = build_tree(extract_drilldown(error_events))["nodes"][1]
    assert node["label"].endswith("⚠️") and "工具报错" in node["detail"] and msg in node["detail"]
    stub = Stub()
    render_from_events(stub, error_events)
    # 树 -> 基线对比图(那一步 detect_anomaly 是好的)-> 报错的 contribute 只给说明不画图
    assert [c[0] for c in stub.calls] == ["graphviz_chart", "altair_chart", "caption"]
    assert "工具报错" in stub.calls[2][1] and msg in stub.calls[2][1]
    assert "比率型" not in stub.calls[2][1]   # 失败不是口径问题:不替它编解释
    both, mixed = {**error, "total_base": 1.0, "total_cmp": 1.0, "change_rate": -0.2}, Stub()
    render_from_events(mixed, cont(0, "gmv", "store_name", both))
    assert [c[0] for c in mixed.calls] == ["graphviz_chart", "caption"]  # 有总量也不许画
    assert "工具报错" in mixed.calls[1][1] and "比率型" not in mixed.calls[1][1]
    ok = Stub()
    render_from_events(ok, events)
    kinds = [c[0] for c in ok.calls]
    assert kinds.count("graphviz_chart") == 1 and kinds[0] == "graphviz_chart"
    # 基线对比(anomaly)+ 区域贡献 + 城市贡献 + 整窗分解
    assert kinds.count("altair_chart") == 4
    ratio = Stub()
    render_from_events(ratio, call(0, "contribute", {"metric": "aov"},
                                    {"metric": "aov", "total_base": 5.0, "total_cmp": 4.0,
                                     "change_rate": -0.2, "top": [{"key": "a",
                                                                   "change_rate": -0.2}]}))
    # 比率型:不再「只说明」—— 说明之后画变化率条形图(它有的就是逐切片变化率)
    assert [c[0] for c in ratio.calls] == ["graphviz_chart", "caption", "altair_chart"]
    assert "不可加" in ratio.calls[1][1]
    empty = Stub()
    assert render_from_events(empty, []) is None and empty.calls == []


def test_render_from_events_hints_on_unknown_result_shape() -> None:
    """结果既不是报错、也没有可画数据、又判不出比率型 -> 如实说「形态无法识别」,不硬画。"""
    stub = Stub()
    render_from_events(stub, call(0, "decompose", {"target": "t"},
                                   {"target": "t", "total_base": 1.0, "total_cmp": 2.0,
                                    "effects": []}))
    assert [c[0] for c in stub.calls] == ["graphviz_chart", "caption"]
    assert "结果形态无法识别" in stub.calls[1][1] and "不画因子分解瀑布图" in stub.calls[1][1]


def _viz_app(events) -> None:
    """AppTest 宿主:源码被原样当脚本执行 —— imports 必须在函数体内,数据经 kwargs 传入。"""
    import streamlit as st

    from attribution_viz import render_from_events

    render_from_events(st, events)


def test_apptest_renders_tree_and_waterfall(events) -> None:
    """真 streamlit:树与瀑布图进元素树、没有异常;空事件流一个元素都不画。"""
    at = AppTest.from_function(_viz_app, default_timeout=30, kwargs={"events": events}).run()
    assert not at.exception, [element.value for element in at.exception]
    graphs = at.get("graphviz_chart")
    assert len(graphs) == 1 and "digraph" in str(graphs[0].spec)
    charts = [*at.get("vega_lite_chart"), *at.get("arrow_vega_lite_chart")]
    assert len(charts) == 4
    specs = [json.loads(c.spec) for c in charts]
    # 首张是基线对比图:分层(柱 + 基线±MAD 的离散带),所以顶层没有 mark;
    # 其余三张是瀑布图,mark 干净地形如 {"type": "bar"}。
    assert specs[0]["layer"][0]["mark"]["type"] == "bar" and len(specs[0]["layer"]) == 2
    assert all(s["mark"]["type"] == "bar" for s in specs[1:])

    blank = AppTest.from_function(_viz_app, default_timeout=30, kwargs={"events": []}).run()
    assert not blank.exception, [element.value for element in blank.exception]
    assert not blank.get("graphviz_chart") and not blank.get("vega_lite_chart")
