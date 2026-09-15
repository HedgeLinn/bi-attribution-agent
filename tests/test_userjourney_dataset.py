"""user-journey 数据集的自证测试(§5.4 第三套 6 case / §5.10 隔离与可回验)。

考的不是「能不能下钻」,而是「跨粒度能不能算对」:这是用户级事件流 + 同期群,核心指标
全部破坏「分子分母同粒度」的假设(半可加 DAU、派生 ARPU、两段式比率)。六个必测点:
6 个 case 都能读出来且 `evaluate --mock` 出报告 / U1 渠道总量正常而批次留存断崖(渠道
-14%、批次 -84%,能从数据反着测)/ U4 结构分解恒等(Σ效应 ≡ 总变化)且结构效应主导
(反事实 1707.16 vs 实际 1819.68)/ U5 半可加 dau 返回**最后有数据日**(544)而非逐日
之和(3760)/ 4 个切片根因的受影响集合从数据现算且两两不交 / U6 批次回填可反向取证
(事件日 ≠ 入库日,150 人的记录注册日 > 首次事件日)。离线可跑,不需要 BI_API_KEY:
`python -m pytest tests/test_userjourney_dataset.py -q`
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
DATASET = REPO / "datasets" / "user-journey"
MONTHS = tuple((f"2026-{m:02d}-01", f"2026-{m:02d}-{d}") for m, d in
               ((1, 31), (2, 28), (3, 31), (4, 30), (5, 31), (6, 30)))      # 1~6 月
MAY, JUN = MONTHS[4], MONTHS[5]
WEEK1, WEEK2 = ("2026-06-01", "2026-06-07"), ("2026-06-08", "2026-06-14")

CASE_IDS = frozenset({
    "u1_batch_cliff", "u2_reg_funnel", "u3_version_cliff",
    "u4_arpu_structural", "u5_dau_caliber", "u6_cohort_backfill",
})
# 切片根因的 4 条(case -> 维度 / 层级字段 / 键);U5 / U6 是「无法归因」类(根因切片为 null)
SLICE_ROOTS: dict[str, tuple[str, str, str]] = {
    "u1_batch_cliff": ("batch", "batch_id", "CH_SOCIAL-2026-01"),
    "u2_reg_funnel": ("channel", "channel_id", "CH_ADS"),
    "u3_version_cliff": ("version", "app_version", "V4.2.0"),
    "u4_arpu_structural": ("tier", "tier_id", "低价值"),
}
NULL_ROOT_CASES = CASE_IDS - set(SLICE_ROOTS)
# 数值维:只有两条切片根因能给出「诚实的 agent 手上也拿得到的贡献度」,其余为 null
VALUE_RANGES = {"u1_batch_cliff": (0.55, 0.95), "u3_version_cliff": (0.65, 1.05)}
# 受影响集合从**数据**现算:case -> (指标, 维度层级字段, 基期起, 基期止, 对比期起, 对比期止);
# 两条 null-root 类不进这张表(没有切片根因,其「允许叠加」由 overlap_with 显式声明)
PLANTED: dict[str, tuple[str, str, str, str, str, str]] = {
    "u1_batch_cliff": ("active_users", "batch_id", "2026-04-01", "2026-04-30", "2026-05-01", "2026-05-31"),
    "u2_reg_funnel": ("registrations", "channel_id", "2026-02-01", "2026-02-28", "2026-03-01", "2026-03-31"),
    "u3_version_cliff": ("active_users", "app_version", "2026-03-08", "2026-04-07", "2026-04-08", "2026-05-08"),
    "u4_arpu_structural": ("active_users", "tier_id", "2026-05-01", "2026-05-31", "2026-06-01", "2026-06-30"),
}


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


def _months(engine: AttributionEngine, filters: dict) -> list[float]:
    """切片在 1~6 月的逐月活跃用户 —— 同期群口径下这条曲线就是留存曲线。"""
    return [_total(engine, "active_users", filters, *month) for month in MONTHS]


def _impact_moves(engine: AttributionEngine, metric: str, dim: str,
                  base: tuple[str, str], cmp: tuple[str, str]) -> dict[str, float]:
    """该维度上每个键的**绝对变化**;两窗任一为 None 的键排除(缺行不是「变了」)。

    刻意用绝对变化:相对口径下「1 人 → 0 人」的小同期群(-100%)会把上千人批次塌 90 人挤掉。
    """
    before = {row[dim]: row["value"] for row in engine.query_metric(metric, [dim], {}, *base)["rows"]}
    after = {row[dim]: row["value"] for row in engine.query_metric(metric, [dim], {}, *cmp)["rows"]}
    moves = {key: abs(after[key] - before[key]) for key in set(before) & set(after)
             if before[key] is not None and after[key] is not None}
    assert moves, f"{metric} 在 {base} → {cmp} 上没有任何可比键"
    return moves


# --- 1. 6 个 case 都能读出来,mock 评估能出报告 -----------------------------
def test_all_cases_load_and_mock_report_runs():
    cases = {case.id: case for case in load_cases(str(DATASET))}
    assert set(cases) == CASE_IDS, "case 文件与本文档的清单必须一一对应"
    for case_id, case in cases.items():
        assert case.tier in {"L2", "L3", "L4"} and case.category in {"切片下钻", "漏斗", "跨粒度", "数据质量"}
        assert case.question.strip() and len(case.must_not_claim) >= 3, case_id
        # 派生/比率指标的 contribute 贡献度恒为 null,声明区间会误伤诚实答案
        assert case.contribution_range == VALUE_RANGES.get(case_id), case_id
        if case_id in NULL_ROOT_CASES:
            assert case.root_cause_slice is None, f"{case_id} 是「无法归因」类"
        else:
            dimension, level, key = SLICE_ROOTS[case_id]
            occupancy = case.raw["slice_occupancy"]
            assert case.root_cause_slice["key"] == key
            assert case.root_cause_slice["level"] == case.required_depth == level
            assert occupancy["dimension"] == dimension and list(occupancy["keys"]) == [key]
    # mock 模式:评分管线能跑通并产出可序列化报告(准确率无模型能力含义)
    report = evaluate(str(DATASET), mock=True)
    assert (report["mode"], report["total"]) == ("mock", 6)
    assert json.loads(json.dumps(report))["total"] == 6, "报告必须可 JSON 序列化"
    assert 0 < report["passed"] < 6, "脚本轮换里既有正确也有错误结论,不该全过或全败"
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


# --- 3. U1:渠道总量正常,批次留存断崖(反向检测)----------------------------
def test_u1_channel_normal_but_batch_retention_cliffs(engine, con):
    """同一份数据,渠道口径 -9%、批次口径 -74% —— 必须下钻到「渠道 × 批次」那一格。"""
    batch, channel = "CH_SOCIAL-2026-01", "CH_SOCIAL"
    curve = _months(engine, {"batch_id": batch})
    assert curve == [151.0, 148.0, 138.0, 121.0, 31.0, 19.0], f"批次留存曲线:{curve}"
    assert curve[3] / curve[0] > 0.75, "1~4 月只温和衰减(留存正常)"
    assert curve[4] / curve[3] < 0.30, "5 月一个月掉掉 74% —— 断崖"
    # 对照批次:同月、同渠道结构,却只是自然衰减 —— 不是「2026-01 批次整体不行」
    for peer, peer_curve in {"CH_APPSTORE-2026-01": [233.0, 229.0, 211.0, 167.0, 110.0, 81.0],
                             "CH_ADS-2026-01": [193.0, 183.0, 185.0, 153.0, 101.0, 72.0]}.items():
        assert _months(engine, {"batch_id": peer}) == peer_curve, peer
        assert peer_curve[4] / peer_curve[3] > 0.6, f"{peer} 的 5 月衰减应远小于事发批次"
    # 渠道口径把这格稀释掉:总量只是小回落,异常检测也报不出来
    channel_curve = _months(engine, {"channel_id": channel})
    assert channel_curve == [821.0, 808.0, 841.0, 849.0, 771.0, 757.0], f"渠道曲线:{channel_curve}"
    assert abs(channel_curve[4] - channel_curve[3]) / channel_curve[3] < 0.15
    # 从原始 parquet 现算(不经引擎):同一周对比,批次 -84% 而渠道 -14%
    def week(column, value):
        sql = ("SELECT COUNT(DISTINCT CASE WHEN day BETWEEN '2026-04-24' AND '2026-04-30' THEN user_id END),"
               " COUNT(DISTINCT CASE WHEN day BETWEEN '2026-05-01' AND '2026-05-07' THEN user_id END)"
               f" FROM read_parquet('{_parquet('events')}') WHERE {column} = ?")
        return con.execute(sql, [value]).fetchone()
    batch_weeks, channel_weeks = week("batch_id", batch), week("channel_id", channel)
    assert batch_weeks == (49, 8) and channel_weeks == (441, 379)
    assert (batch_weeks[1] - batch_weeks[0]) / batch_weeks[0] < -0.80
    assert (channel_weeks[1] - channel_weeks[0]) / channel_weeks[0] > -0.20


# --- 4. U4:结构分解恒等 + 结构效应主导(反向辛普森)------------------------
def test_u4_structural_decomposition_identity_and_dominance(engine, con):
    dec = engine.decompose("arpu", ["user_mix", "user_value"], *MAY, *JUN)
    assert dec["kind"] == "structural" and dec["target"] == "arpu"
    effects = {item["factor"]: item for item in dec["effects"]}
    assert set(effects) == {"__mix__", "__rate__"}, "结构化分解必须给出结构效应与自身效应"
    mix, own = effects["__mix__"], effects["__rate__"]
    # 恒等式:Σ效应 ≡ 总变化(零残差)
    assert sum(item["effect"] for item in dec["effects"]) == pytest.approx(dec["total_change"], abs=1e-6)
    assert dec["total_change"] == pytest.approx(dec["total_cmp"] - dec["total_base"], abs=1e-6)
    assert dec["total_change"] / dec["total_base"] == pytest.approx(0.04278, abs=0.001)
    # 结构效应主导:比整体升幅还大,且自身效应是负的 —— 「人均上升 = 用户变值钱」被证伪
    assert mix["effect"] > dec["total_change"] > 0 > own["effect"], (mix["effect"], own["effect"])
    assert mix["contribution"] > 1.0 and abs(mix["effect"]) > 2.5 * abs(own["effect"])
    # 独立反事实(完全不读引擎的分解):5 月权重 × 6 月各层人均 = 1707.16,与实际差 112.53
    rows = con.execute(
        f"SELECT tier_id,"
        f" SUM(CASE WHEN day BETWEEN '2026-05-01' AND '2026-05-31' THEN amount END),"
        f" COUNT(DISTINCT CASE WHEN day BETWEEN '2026-05-01' AND '2026-05-31' THEN user_id END),"
        f" SUM(CASE WHEN day BETWEEN '2026-06-01' AND '2026-06-30' THEN amount END),"
        f" COUNT(DISTINCT CASE WHEN day BETWEEN '2026-06-01' AND '2026-06-30' THEN user_id END)"
        f" FROM read_parquet('{_parquet('events')}') GROUP BY 1").fetchall()
    assert len(rows) == 3, "tier 维度应当恰好三层"
    base_users = sum(row[2] for row in rows)
    counterfactual = sum((row[2] / base_users) * (row[3] / row[4]) for row in rows)
    actual = sum(row[3] for row in rows) / sum(row[4] for row in rows)
    assert counterfactual == pytest.approx(1707.1556, abs=0.5)
    assert actual == pytest.approx(dec["total_cmp"], abs=0.01)
    assert actual - counterfactual == pytest.approx(mix["effect"], rel=0.02)
    # 结构效应的来源:低价值用户占比 25.76% → 20.57%,而他们的人均只有整体的 1/10
    assert {row[0]: row[2] / base_users for row in rows}["低价值"] == pytest.approx(0.2576, abs=0.002)
    assert _total(engine, "active_users", {"tier_id": "低价值"}, *MAY) == 1149
    assert _total(engine, "active_users", {"tier_id": "低价值"}, *JUN) == 854
    assert _total(engine, "active_users", {"tier_id": "高价值"}, *JUN) == 1228, "高价值层几乎没动"
    # 分子其实在缩:人均上升只是分母掉得更快
    assert _rate(engine, "revenue", {}, MAY, JUN) < -0.02
    assert _rate(engine, "active_users", {}, MAY, JUN) < -0.05


# --- 5. U5:半可加指标的取值语义(末值 ≠ 求和 ≠ 去重)----------------------
def test_u5_dau_returns_last_day_not_sum(engine):
    whole = engine.query_metric("dau", [], {}, "2026-06-01", "2026-06-14")
    grouped = engine.query_metric("dau", ["day"], {}, "2026-06-01", "2026-06-14")
    daily = [_total(engine, "dau", {}, f"2026-06-{i:02d}", f"2026-06-{i:02d}") for i in range(1, 15)]
    assert daily[:7] == [500.0, 563.0, 703.0, 521.0, 533.0, 524.0, 532.0]
    assert daily[7:] == [538.0, 559.0, 516.0, 539.0, 523.0, 541.0, 544.0]
    # 引擎口径:整窗 = 最后有数据日 = 544;加 day 维度也只有一行,total 为 None
    assert whole["total"] == 544.0
    assert grouped["rows"] == [{"day": "2026-06-14", "value": 544.0}]
    assert grouped["total"] is None
    assert whole["total"] == daily[-1] != sum(daily[7:]) == 3760.0 and sum(daily[:7]) == 3876.0
    # 三条口径给出三个方向/量级 —— 只有去重口径是对的
    assert (daily[13] - daily[6]) / daily[6] > 0.02, "末值口径:+2.26%(陷阱,方向是反的)"
    assert sum(daily[7:]) < sum(daily[:7]) * 0.99, "逐日求和:-2.99%(方向对,但 3876/3760 是人日)"
    assert _rate(engine, "active_users", {}, WEEK1, WEEK2) == pytest.approx(-0.0351, abs=0.002)
    # 6-03 的单日脉冲(703,比前一日 563 高 25%、比后一日 521 高 35%)同属口径错误
    assert daily[2] > 1.2 * daily[1] and daily[2] > 1.3 * daily[3]


# --- 6. 切片 × 时间隔离:受影响集合两两不交(从数据现算)---------------------
def test_slice_impact_sets_are_pairwise_disjoint(engine):
    impact: dict[str, set[str]] = {}
    for case_id, (metric, dim, b0, b1, c0, c1) in PLANTED.items():
        moves = _impact_moves(engine, metric, dim, (b0, b1), (c0, c1))
        ranked = sorted(moves.values(), reverse=True)
        assert ranked[0] > 1.2 * ranked[1], f"{case_id} 受影响集合非一枝独秀:{ranked[0]:.0f}/{ranked[1]:.0f}"
        impact[case_id] = {key for key, move in moves.items() if move >= 0.8 * ranked[0]}
        declared = SLICE_ROOTS[case_id][2]
        assert impact[case_id] == {declared}, (
            f"{case_id}:数据里长出来的受影响集合应恰好是 {declared},实际 {impact[case_id]}")
    for left, right in itertools.combinations(sorted(impact), 2):
        assert not impact[left] & impact[right], (
            f"{left} 与 {right} 的受影响集合重叠:{impact[left] & impact[right]}")
    # 占用的「维度 × 键」也两两不交(同维不同键,跨维更不相干)
    occupied = {(SLICE_ROOTS[cid][0], key) for cid, keys in impact.items() for key in keys}
    assert len(occupied) == len(impact) == 4


def test_global_cases_declare_allowed_overlap_and_slice_cases_do_not():
    """§5.10 约束 1:全局量(「无法归因」类)必须显式声明「允许叠加」,切片类则不该有。"""
    cases = {case.id: case for case in load_cases(str(DATASET))}
    for case_id in sorted(NULL_ROOT_CASES):
        overlap = [str(item) for item in cases[case_id].raw.get("overlap_with") or []]
        assert overlap, f"{case_id} 是全局量,必须显式声明与哪些 case 允许叠加"
        assert set(overlap) <= CASE_IDS - {case_id}, f"{case_id} 的 overlap_with 含未知 id"
    for case_id in sorted(SLICE_ROOTS):
        assert not (cases[case_id].raw.get("overlap_with") or []), f"{case_id} 的切片是独立的"


# --- 7. U6:批次回填可反着测出来(事件日 ≠ 入库日)--------------------------
def test_u6_backfill_trace_is_detectable_in_reverse(engine, con):
    """记录注册日 > 首次事件日 —— 这批人不是「2 月注册」,是 1 月就活跃、被批次改写了注册日。"""
    events, dim_user = _parquet("events"), _parquet("dim_user")
    backfilled = con.execute(
        f"SELECT COUNT(*), MIN(u.channel_id), MIN(f.first_day), MAX(f.first_day),"
        f" MIN(u.signup_date), MAX(u.signup_date) FROM"
        f" (SELECT user_id, MIN(day) AS first_day FROM read_parquet('{events}') GROUP BY 1) f"
        f" JOIN read_parquet('{dim_user}') u ON f.user_id = u.user_id"
        " WHERE u.signup_date > f.first_day").fetchone()
    assert backfilled == (150, "CH_OFFLINE", "2026-01-05", "2026-01-18",
                          "2026-02-05", "2026-02-05"), backfilled
    # 对照组:其余用户的记录注册日都不晚于首次事件日(库存用户更早 —— 窗口从 2025-06 起)
    assert con.execute(
        f"SELECT COUNT(*) FROM (SELECT user_id, MIN(day) AS first_day FROM"
        f" read_parquet('{events}') GROUP BY 1) f JOIN read_parquet('{dim_user}') u"
        " ON f.user_id = u.user_id WHERE u.signup_date <= f.first_day").fetchone()[0] > 14000
    # 事件发生在 1 月、入库日却统一是 2026-02-05(批次一次性补采):1664 行,ingest_day 只有一个
    assert con.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT ingest_day) FROM read_parquet('{events}')"
        " WHERE day BETWEEN '2026-01-01' AND '2026-01-31' AND ingest_day <> day").fetchone() == (1664, 1)
    assert con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{events}') e JOIN read_parquet('{dim_user}') u"
        " ON e.user_id = u.user_id WHERE u.signup_date = '2026-02-05'"
        " AND e.day BETWEEN '2026-01-01' AND '2026-01-31' AND e.ingest_day <> e.day").fetchone()[0] == 1664
    # 引擎侧:同期群看板把 150 人记成「2026-02 注册」,却在 1 月窗口就出现 —— 自相矛盾
    def cohort(start, end):
        return {row["signup_month"]: row["value"] for row in engine.query_metric(
            "active_users", ["signup_month"], {"channel_id": "CH_OFFLINE"}, start, end)["rows"]}
    january, february = cohort(*MONTHS[0]), cohort(*MONTHS[1])
    assert (january["2026-01"], january["2026-02"]) == (104.0, 150.0)
    assert (february["2026-01"], february["2026-02"]) == (98.0, 229.0)
    assert february["2026-02"] - january["2026-02"] == 79.0, "同期群人数随回填批次漂移"
    # 渠道总量没有翻倍 —— 「2 月地推拉新翻倍」在渠道口径上就不成立
    assert _total(engine, "active_users", {"channel_id": "CH_OFFLINE"}, *MONTHS[0]) == 716
