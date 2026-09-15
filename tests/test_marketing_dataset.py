"""marketing-funnel 数据集的自证测试(§5.4 第二套 14 case / §5.10 隔离与可回验)。

考的不是「能不能下钻」,而是「数据本身可不可信」:8 张表分成平台 / 埋点 / 业务三套口径。
五个必测点:

    1. 14 个 case 都能被评估脚手架读出来,`evaluate --mock` 出一份可 JSON 序列化的报告
    2. 每个坑都有回验期望(≥15 条实测值),`verify_dataset` 全绿;5 个 C 类坑还能从数据
       反着测出来(口径 / 完整性缺口各有一条独立于声明的取证路径)
    3. 切片 × 时间隔离:7 个切片根因的受影响集合从数据现算,两两不交
    4. 跨表口径:平台口径转化之和 > 业务订单(实测比值 1.23,两套口径不可混)
    5. dim_creative.launch_date 存在,且 B1 的 CTR 衰减按投放天数才单调(按日历日看 73 天
       里 31 天在涨 —— 这就是 B1 的陷阱面)

离线可跑:`python -m pytest tests/test_marketing_dataset.py -q`(不需要 BI_API_KEY)
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import duckdb
import pytest

from attribution.engine import AttributionEngine
from scripts.evaluate_agent import evaluate, load_cases
from scripts.verify_dataset import load_expectations, verify

REPO = Path(__file__).resolve().parents[1]
DATASET = REPO / "datasets" / "marketing-funnel"

CASE_IDS = frozenset({
    "b1_ctr_decay", "b2_cpc_surge", "b3_cvr_cliff", "b4_budget_cap", "b5_frequency_fatigue",
    "b6_creative_rejected", "c1_conv_backfill", "c2_conv_overclaim", "c3_timezone_shift",
    "c4_tracking_gap", "c5_pause_gap", "d1_promo_hangover", "d2_brand_trap", "d3_correlation",
})
# 切片根因的 7 条(case -> 维度 / 层级字段 / 键);其余 7 条是「无法归因」类(根因切片为 null)
SLICE_ROOTS: dict[str, tuple[str, str, str]] = {
    "b1_ctr_decay": ("creative", "creative_id", "CR_TT_007"),
    "b2_cpc_surge": ("audience", "audience_id", "AUD_XHS_02"),
    "b3_cvr_cliff": ("campaign", "campaign_id", "CAMP_TM_02"),
    "b4_budget_cap": ("campaign", "campaign_id", "CAMP_WX_02"),
    "b5_frequency_fatigue": ("audience", "audience_id", "AUD_DY_02"),
    "b6_creative_rejected": ("creative", "creative_id", "CR_XHS_004"),
    "c5_pause_gap": ("campaign", "campaign_id", "CAMP_JD_01"),
}
NULL_ROOT_CASES = CASE_IDS - set(SLICE_ROOTS)
# 受影响集合从**数据**现算:case -> (指标, 维度层级字段, 基期起, 基期止, 对比期起, 对比期止);窗口与
# cases/*.yaml 注释块、expectations.yaml 对齐;c5 走「停投缺行」另一条路(见 test_slice_impact_*)
PLANTED: dict[str, tuple[str, str, str, str, str, str]] = {
    "b1_ctr_decay": ("ctr", "creative_id", "2026-06-20", "2026-07-04", "2026-07-15", "2026-07-29"),
    "b2_cpc_surge": ("cpc", "audience_id", "2026-04-22", "2026-05-11", "2026-05-12", "2026-05-31"),
    "b3_cvr_cliff": ("cvr", "campaign_id", "2026-03-09", "2026-03-15", "2026-03-16", "2026-03-22"),
    "b4_budget_cap": ("ad_cost", "campaign_id", "2026-03-01", "2026-03-30", "2026-04-01", "2026-04-30"),
    "b5_frequency_fatigue": ("frequency", "audience_id", "2026-07-28", "2026-08-10", "2026-08-11", "2026-08-24"),
    "b6_creative_rejected": ("ad_impr", "creative_id", "2026-07-22", "2026-08-04", "2026-08-05", "2026-08-18"),
}
PAUSE_WINDOW = ("2026-07-05", "2026-07-18")     # C5 停投期
PAUSE_BEFORE = ("2026-06-21", "2026-07-04")     # 停投前 14 天
PAID = {"is_paid": "付费"}                      # 业务口径的付费订单(两套口径里的「业务侧」)


@pytest.fixture(scope="module")
def engine() -> AttributionEngine:
    """真实数据 + 真实语义层的引擎(模块级:它只读,不写)。"""
    return AttributionEngine(str(DATASET / "data"), str(DATASET / "semantic.yaml"))


@pytest.fixture(scope="module")
def con():
    connection = duckdb.connect()
    yield connection
    connection.close()


def _parquet(table: str) -> str:
    return (DATASET / "data" / f"{table}.parquet").as_posix()


def _total(engine: AttributionEngine, metric: str, filters: dict, start: str, end: str):
    """窗口标量(dims 为空时 total 才有意义);整窗无行 → None,不是 0。"""
    return engine.query_metric(metric, [], dict(filters), start, end)["total"]


def _rate(engine: AttributionEngine, metric: str, filters: dict,
          base: tuple[str, str], cmp: tuple[str, str]) -> float:
    """两窗变化率(引擎实测);任一窗无值直接失败,不许静默当成 0。"""
    before, after = _total(engine, metric, filters, *base), _total(engine, metric, filters, *cmp)
    assert before is not None and after is not None, f"{metric} 窗口无值:{before} → {after}"
    return (after - before) / before


def _rows(con, table: str, start: str, end: str,
          where: str = "1=1", params: list | None = None) -> int:
    """原始 parquet 的行数(绕开引擎直接数行 —— 「没有行」只有这一条路能证)。"""
    sql = f"SELECT COUNT(*) FROM read_parquet('{_parquet(table)}') WHERE date_id BETWEEN ? AND ?"
    return con.execute(f"{sql} AND {where}", [start, end] + list(params or [])).fetchone()[0]


def _impact_set(engine: AttributionEngine, metric: str, dim: str,
                base: tuple[str, str], cmp: tuple[str, str], share: float = 0.8) -> set[str]:
    """受影响集合 = 该维度上「变化主导」的键:两窗都有值,且 |相对变化| ≥ 最大者的 80%。

    两窗任一为 None 的键排除(素材退市 / 人群包换绑 / 停投各有单测),否则缺行会被算成
    无限大的变化,把根因挤掉。"""
    before = {row[dim]: row["value"] for row in engine.query_metric(metric, [dim], {}, *base)["rows"]}
    after = {row[dim]: row["value"] for row in engine.query_metric(metric, [dim], {}, *cmp)["rows"]}
    moves = {key: abs((after[key] - before[key]) / before[key]) for key in set(before) & set(after)
             if before[key] not in (None, 0) and after[key] is not None}
    assert moves, f"{metric} 在 {base} → {cmp} 上没有任何可比键"
    return {key for key, move in moves.items() if move >= share * max(moves.values())}


# --- 1. 14 个 case 都能读出来,mock 评估能出报告 -----------------------------
def test_all_cases_load_and_mock_report_runs():
    cases = {case.id: case for case in load_cases(str(DATASET))}
    assert set(cases) == CASE_IDS, "case 文件与本文档的清单必须一一对应"
    for case_id, case in cases.items():
        assert case.tier == "L2" and case.category in {"漏斗", "数据质量", "归因陷阱"}
        assert case.question.strip() and len(case.must_not_claim) >= 3, case_id
        # 数值维:14 条全为 null —— 7 条根因是比率指标(contribute 恒返回 null),7 条是
        # 「无法归因」类(没有根因切片);声明区间反而会误伤诚实答案
        assert case.contribution_range is None, case_id
        occupancy = case.raw["slice_occupancy"]
        if case_id in NULL_ROOT_CASES:
            assert case.root_cause_slice is None, f"{case_id} 是「无法归因」类"
        else:
            dimension, level, key = SLICE_ROOTS[case_id]
            assert case.root_cause_slice["key"] == key
            assert case.root_cause_slice["level"] == case.required_depth == level
            assert occupancy["dimension"] == dimension and list(occupancy["keys"]) == [key]
    # mock 模式:评分管线能跑通并产出可序列化报告(准确率无模型能力含义)
    report = evaluate(str(DATASET), mock=True)
    assert (report["mode"], report["total"]) == ("mock", 14)
    assert json.loads(json.dumps(report))["total"] == 14, "报告必须可 JSON 序列化"
    assert 0 < report["passed"] < 14, "脚本轮换里既有正确也有错误结论,不该全过或全败"
    dimensions = {"定位命中", "深度达标", "数值准确", "无错误断言"}
    assert all(set(record["dimensions"]) == dimensions for record in report["cases"])


# --- 2. 每个坑都有回验期望,且回验全绿 --------------------------------------
def test_every_pit_has_expectation_and_reverification_is_green():
    spec = load_expectations(str(DATASET))
    expectations = spec["expectations"]
    assert spec["dataset_version"] == "1.0.0" and len(expectations) >= 15
    wanted = {case_id.split("_")[0] for case_id in CASE_IDS}
    covered = {item["id"].split("_")[0] for item in expectations}
    assert wanted <= covered, f"有坑没有回验期望:{sorted(wanted - covered)}"
    kinds = {"metric_change", "slice_absent", "appeared"}
    assert all(item["description"] and item["check"]["type"] in kinds for item in expectations)
    report = verify(str(DATASET))
    assert report["dataset_version"] == "1.0.0"      # 绑定语义层数据集版本
    assert report["total"] == len(expectations) >= 15
    assert report["passed"] == report["total"], "回验有未通过项"


def test_c_class_pits_detectable_in_reverse_from_data(engine, con):
    """C 类(数据质量)5 条坑各自的独立取证路径 —— 都不读 case 声明。"""
    # C1 平台回传按比例缺失:平台口径 -70.57%,同窗点击 / 订单 / 埋点会话都正常,五渠道同比例
    week, before_week = ("2026-08-25", "2026-08-31"), ("2026-08-18", "2026-08-24")
    assert _rate(engine, "ad_conv", {}, before_week, week) == pytest.approx(-0.7057, abs=0.01)
    for metric in ("ad_clicks", "biz_orders", "track_sessions"):
        assert abs(_rate(engine, metric, {}, before_week, week)) < 0.05, metric
    per_channel = {row["channel_id"]: row["value"]
                   for row in engine.query_metric("ad_conv", ["channel_id"], {}, *week)["rows"]}
    assert len(per_channel) == 5
    for channel, value in per_channel.items():
        same = engine.query_metric("ad_conv", ["channel_id"], {"channel_id": channel},
                                   *before_week)["rows"][0]["value"]
        assert -0.80 < (value - same) / same < -0.60, channel
    # 缺数周平台口径反而低于业务订单 —— 两套口径的关系被翻转,只有「回传缺失」解释得通
    assert sum(per_channel.values()) < _total(engine, "biz_orders", {}, *week)

    # C2 两套口径恒定比值(实测 1.2300 / 1.2302),且**同向同幅**下滑
    months = (("2026-06-01", "2026-06-30"), ("2026-07-01", "2026-07-31"))
    ratios = [_total(engine, "ad_conv", {}, *m) / _total(engine, "biz_orders", PAID, *m) for m in months]
    assert ratios == [pytest.approx(1.2300, abs=0.005), pytest.approx(1.2302, abs=0.005)]
    assert max(ratios) - min(ratios) < 0.01, f"恒定比值,不该漂移:{ratios}"

    # C3 两套日界错位:一日反向暴走,两天合计两表吻合,私域水位前后没变
    priv, days = {"channel_id": "CH_PRIV"}, ("2026-04-21", "2026-04-22")
    orders = [_total(engine, "biz_orders", priv, day, day) for day in days]
    sessions = [_total(engine, "track_sessions", priv, day, day) for day in days]
    assert orders[0] > 3 * orders[1] and sessions[1] > 3 * sessions[0], "订单在 UTC 日、会话在北京日"
    two_day = [_rate(engine, metric, priv, ("2026-04-19", "2026-04-20"), days)
               for metric in ("biz_orders", "track_sessions")]
    assert abs(two_day[0] - two_day[1]) < 0.05, f"两天合计两表应当吻合:{two_day}"
    watermark = [_total(engine, "biz_orders", priv, start, end) / 7
                 for start, end in (("2026-04-14", "2026-04-20"), ("2026-04-23", "2026-04-29"))]
    assert abs(watermark[1] - watermark[0]) / watermark[0] < 0.05, watermark

    # C4 埋点整段缺行(不是 0):触点数 0 行,而同一窗口投放(2 计划 × 3 天 = 6 行)与订单都正常
    gap = ("2026-05-04", "2026-05-06")
    assert _rows(con, "touchpoints", *gap, "channel_id = ?", ["CH_JD"]) == 0
    assert _rows(con, "touchpoints", "2026-05-01", "2026-05-03", "channel_id = ?", ["CH_JD"]) == 3
    assert _rows(con, "ad_daily", *gap, "channel_id = ?", ["CH_JD"]) == 6
    assert _rows(con, "orders", *gap, "channel_id = ?", ["CH_JD"]) > 0
    assert _total(engine, "track_sessions", {"channel_id": "CH_JD"}, *gap) is None, "没有行 → NULL"

    # C5 停投空白期:两张表都没有行(不是 0),而同一条「没有行」在两种聚合下表现不同 ——
    #    SUM(消耗)= NULL,COUNT(订单)= 0
    assert _rows(con, "ad_daily", *PAUSE_WINDOW, "campaign_id = ?", ["CAMP_JD_01"]) == 0
    assert _rows(con, "orders", *PAUSE_WINDOW, "campaign_id = ?", ["CAMP_JD_01"]) == 0
    assert _total(engine, "ad_cost", {"campaign_id": "CAMP_JD_01"}, *PAUSE_WINDOW) is None
    assert _total(engine, "biz_orders", {"campaign_id": "CAMP_JD_01"}, *PAUSE_WINDOW) == 0


# --- 3. 切片 × 时间隔离:受影响集合两两不交(从数据现算)---------------------
def test_slice_impact_sets_are_pairwise_disjoint(engine, con):
    impact: dict[str, set[str]] = {}
    for case_id, (metric, dim, b0, b1, c0, c1) in PLANTED.items():
        impact[case_id] = _impact_set(engine, metric, dim, (b0, b1), (c0, c1))
        declared = SLICE_ROOTS[case_id][2]
        assert impact[case_id] == {declared}, (
            f"{case_id}:数据里长出来的受影响集合应恰好是 {declared},实际 {impact[case_id]}")
    # c5 的影响形态是「缺行」不是「变化」:停投前有行的计划里,停投期没有行的只有一个
    sql = (f"SELECT DISTINCT campaign_id FROM read_parquet('{_parquet('ad_daily')}')"
           " WHERE date_id BETWEEN ? AND ?")
    present_before = {row[0] for row in con.execute(sql, list(PAUSE_BEFORE)).fetchall()}
    present_in_pause = {row[0] for row in con.execute(sql, list(PAUSE_WINDOW)).fetchall()}
    impact["c5_pause_gap"] = present_before - present_in_pause
    assert impact["c5_pause_gap"] == {"CAMP_JD_01"}
    for left, right in itertools.combinations(sorted(impact), 2):
        assert not impact[left] & impact[right], (
            f"{left} 与 {right} 的受影响集合重叠:{impact[left] & impact[right]}")
    # 占用的「维度 × 键」也两两不交(同维不同键,跨维更不相干)
    occupied = {(SLICE_ROOTS[cid][0], key) for cid, keys in impact.items() for key in keys}
    assert len(occupied) == len(impact) == 7


def test_global_cases_declare_allowed_overlap_and_slice_cases_do_not():
    """§5.10 约束 1:全局量(「无法归因」类)必须显式声明「允许叠加」,切片类则不该有。"""
    cases = {case.id: case for case in load_cases(str(DATASET))}
    for case_id in sorted(NULL_ROOT_CASES):
        overlap = [str(item) for item in cases[case_id].raw.get("overlap_with") or []]
        assert overlap, f"{case_id} 是全局量,必须显式声明与哪些 case 允许叠加"
        assert set(overlap) <= CASE_IDS - {case_id}, f"{case_id} 的 overlap_with 含未知 id"
    for case_id in sorted(SLICE_ROOTS):
        assert not (cases[case_id].raw.get("overlap_with") or []), f"{case_id} 的切片是独立的"


# --- 4. 跨表口径:平台口径转化之和 > 业务订单(实测 +23%)---------------------
def test_platform_calibre_exceeds_business_orders(engine, con):
    """两套口径不可混:平台口径的转化是回传值,比业务付费订单恒多 ~23%。"""
    june, july = ("2026-06-01", "2026-06-30"), ("2026-07-01", "2026-07-31")
    for month in (june, july):
        platform, business = _total(engine, "ad_conv", {}, *month), _total(
            engine, "biz_orders", PAID, *month)
        assert platform > business * 1.20, f"{month}:平台口径应比业务订单多 20% 以上"
    # 直接从 parquet 现算同一个事实(不经引擎):平台回传是广告表里的独立字段
    ad_conv = con.execute(f"SELECT SUM(platform_conv) FROM read_parquet('{_parquet('ad_daily')}')"
                          " WHERE date_id BETWEEN ? AND ?", list(july)).fetchone()[0]
    paid = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{_parquet('orders')}') o"
        f" JOIN read_parquet('{_parquet('dim_campaign')}') d ON o.campaign_id = d.campaign_id"
        " WHERE o.date_id BETWEEN ? AND ? AND d.is_paid = '付费'", list(july)).fetchone()[0]
    assert (ad_conv, paid) == (33165, 26960), "7 月实测:平台口径多 6,205 单(+23.02%)"
    assert ad_conv / paid == pytest.approx(1.2302, abs=0.005)
    # 业务侧还有零消耗的自然流量订单,全量订单更高 —— 两套口径的分母根本不是一回事
    assert _total(engine, "biz_orders", {}, *july) > paid, "全量订单应含自然流量订单"


# --- 5. B1:按「投放天数」才单调(日历视角毫无规律)-------------------------
def test_b1_decay_is_visible_only_by_age_buckets(engine, con):
    """素材的衰减是对**自己的上线天数**成立,不是对日历日期成立。"""
    # 5.1 dim_creative 必须带 launch_date;B1 的素材 2026-05-20 上线、2026-07-31 退市
    ad_daily, dim_creative = _parquet("ad_daily"), _parquet("dim_creative")
    columns = [row[0] for row in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{dim_creative}')").fetchall()]
    assert "launch_date" in columns, f"dim_creative 缺 launch_date:{columns}"
    launch = con.execute(f"SELECT launch_date FROM read_parquet('{dim_creative}')"
                         " WHERE creative_id = 'CR_TT_007'").fetchone()[0]
    assert launch == "2026-05-20"
    # 5.2 按 7 天一个年龄段聚合 CTR:满 12 天之后**段段下降**
    buckets = con.execute(
        f"SELECT datediff('day', CAST(d.launch_date AS DATE), CAST(a.date_id AS DATE)) // 7 AS b,"
        f" SUM(a.clicks) / SUM(a.impressions) AS ctr FROM read_parquet('{ad_daily}') a"
        f" JOIN read_parquet('{dim_creative}') d ON a.creative_id = d.creative_id"
        " WHERE a.creative_id = 'CR_TT_007' GROUP BY 1 ORDER BY 1").fetchall()
    ctrs = [float(row[1]) for row in buckets]
    assert len(ctrs) == 11, "05-20 上线、07-31 退市,73 天 = 11 个 7 天段"
    stable, decaying = ctrs[:2], ctrs[2:]      # 段 0~1 = 上线前 14 天(稳定期)
    assert abs(stable[1] - stable[0]) / stable[0] < 0.05, f"稳定期应当平:{stable}"
    assert all(later < earlier for earlier, later in zip(decaying, decaying[1:])), (
        f"上线满 12 天后必须段段下降:{decaying}")
    assert decaying[0] > 2.5 * decaying[-1], "从 ~2.81% 衰减到 ~0.98%"
    # 5.3 按日历日看则毫无规律:73 天里 31 天环比在涨(单调递减的序列不可能这样)
    daily = con.execute(f"SELECT SUM(clicks) / SUM(impressions) AS ctr"
                        f" FROM read_parquet('{ad_daily}') WHERE creative_id = 'CR_TT_007'"
                        " GROUP BY date_id ORDER BY date_id").fetchall()
    values = [float(row[0]) for row in daily]
    up_moves = sum(1 for earlier, later in zip(values, values[1:]) if later > earlier)
    assert (len(values), up_moves) == (73, 31), f"实测 {len(values)} 天 / {up_moves} 天在涨"
    # 5.4 引擎侧同样成立:同计划老素材 CR_TT_001 的 CTR 不动(对照 —— 不是大盘在变)
    windows = (("2026-06-20", "2026-07-04"), ("2026-07-15", "2026-07-29"))
    assert _rate(engine, "ctr", {"creative_id": "CR_TT_007"}, *windows) < -0.35
    assert abs(_rate(engine, "ctr", {"creative_id": "CR_TT_001"}, *windows)) < 0.05
