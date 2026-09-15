"""saas-mrr 数据集的自证测试(§5.6 半可加 / §4.2 加法分解 / §5.10 隔离与可回验)。

这套数据集存在的意义就是用最小成本验证 schema v2 的两个新表达能力,所以测试只钉
两件事在**真实数据**上成立,外加三条数据本身的铁律:

    1. 半可加语义:多日窗口的取值 = 窗口内最后有数据日的水平,不是逐日求和
       (两者相差 75.5 倍,断言消息里两个数都写出来)
    2. 加法分解的恒等式:Σ效应 ≡ 总变化(1e-6 相对容差),且只有同原点的两段窗口成立
       (不相交的月份必须抛 DecomposeError,不许静默算错)
    3. 四个 case 都能被评估脚手架读出来(坏 case 不许静默跳过),窗口与注释块一致
    4. 铁律一:任意一天的快照水平合计 ≡ 该日及以前的全部变动(逐笔整数,差恒为 0)
    5. 铁律二:S1–S4 的受影响账户集合两两不交 —— **从数据里现算**,不读 case 里的声明
       (只信「数据里长出来的主因」,否则「以为隔离了其实没有」永远发现不了)

离线可跑(不需要 BI_API_KEY):
    python -m pytest tests/test_saas_dataset.py -q
"""

from __future__ import annotations

import itertools
from pathlib import Path

import duckdb
import pytest

from attribution.decompose import DecomposeError
from attribution.engine import AttributionEngine
from scripts.evaluate_agent import load_cases

REPO = Path(__file__).resolve().parents[1]
DATASET = REPO / "datasets" / "saas-mrr"
FACT_TABLE, MOVEMENT_TABLE, ACCOUNT_TABLE = "subscriptions", "mrr_movements", "dim_account"

FACTORS = ("new_mrr", "expansion_mrr", "contraction_mrr", "churn_mrr")
ORIGIN = "2025-07-01"        # 数据起点 = 水平零点(§4.2 的恒等式要求两段窗口同原点)
FINAL_DAY = "2026-06-30"

# case -> (基期窗口, 对比期窗口):与 cases/*.yaml 的注释块、slice_occupancy 对齐
CASES: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    "s1_churn": (("2026-05-01", "2026-05-31"), ("2026-06-01", "2026-06-30")),
    "s2_expansion": (("2026-03-01", "2026-03-31"), ("2026-04-01", "2026-04-30")),
    "s3_sum_trap": (("2026-04-01", "2026-04-30"), ("2026-05-01", "2026-05-31")),
    "s4_churn_lag": (("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28")),
}
# case -> (主因流指标, 埋点窗口):受影响账户从这张指标上现算(见 _dominant_accounts)
PLANTED: dict[str, tuple[str, str, str]] = {
    "s1_churn": ("churn_mrr", "2026-06-01", "2026-06-30"),
    "s2_expansion": ("expansion_mrr", "2026-03-01", "2026-03-31"),
    "s3_sum_trap": ("contraction_mrr", "2026-05-01", "2026-05-31"),
    "s4_churn_lag": ("churn_mrr", "2026-02-01", "2026-02-28"),
}
# 对账用的「任意日期」:月初 / 月末 / 月中 / 埋点日附近,避开整月对齐的巧合
RECON_DAYS = ("2025-07-31", "2026-02-28", "2026-03-17", "2026-05-13", "2026-06-30")


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


def _scalar(con, sql: str, params: list) -> object:
    return con.execute(sql, params).fetchone()[0]


def _dominant_accounts(engine: AttributionEngine, metric: str, start: str, end: str,
                       share: float = 0.5) -> set[str]:
    """该窗口里吃下指标流量一半以上的账户(受影响集合**从数据现算**,不读 case 声明)。"""
    total = engine.query_metric(metric, [], {}, start, end)["total"]
    rows = engine.query_metric(metric, ["account_id"], {}, start, end)["rows"]
    return {row["account_id"] for row in rows
            if row["value"] and abs(row["value"]) >= share * abs(total)}


# --- 1. 半可加:窗口 = 末日值,不是逐日求和 ---------------------------------
def test_semi_additive_window_is_last_day_not_sum(engine, con):
    """跨两个月的窗口取「最后有数据日」的水平:19,527,300 分(195,273 元)。

    逐日求和是 1,474,864,500 分(14,748,645 元)—— 75.5 倍。这就是 §1.3 的静默错误。
    """
    window_end = "2026-05-31"
    window_value = engine.query_metric("mrr", [], {}, "2026-04-01", window_end)["total"]
    last_value = engine.query_metric("mrr", [], {}, window_end, window_end)["total"]
    summed = _scalar(con, f"SELECT SUM(mrr_amount) FROM read_parquet('{_parquet(FACT_TABLE)}')"
                          " WHERE date_id BETWEEN ? AND ?", ["2026-04-01", window_end])
    assert window_value == last_value == 19_527_300.0, "窗口值应等于最后有数据日的水平"
    assert summed == 1_474_864_500.0, "逐日求和的实测值(与窗口值相差 75.5 倍)"
    assert window_value != summed
    assert summed / window_value == pytest.approx(75.53, abs=0.01)

    # 分组聚合同样是「每组各自的末日值」,不是每组求和
    rows = engine.query_metric("mrr", ["plan"], {}, "2026-04-01", window_end)["rows"]
    by_plan = {row["plan"]: row["value"] for row in rows}
    assert by_plan == {"专业版": 7_774_000.0, "企业版": 2_398_800.0, "基础版": 1_673_100.0,
                       "旗舰版": 7_292_700.0, "试用版": 388_700.0}
    for plan, value in by_plan.items():
        single = engine.query_metric("mrr", ["plan"], {"plan": plan},
                                    window_end, window_end)["rows"]
        assert single and single[0]["value"] == value, f"{plan} 的窗口值应等于其末日值"


def test_churned_account_has_no_rows_but_engine_returns_none(engine, con):
    """口径缺口的现场(数据集的意义之一就是把它暴露出来,故两条都钉住):

    - 数据集侧的**契约**:ACC_E001 自流失日(2026-02-20)起快照里一行都没有,而它的
      累计变动恰好归零 —— 没有行 = 水平 0(caveat 第 3 条)。
    - 引擎侧的**现状**:整窗无行时标量返回 None(不是 0.0),于是 expectations 里的
      metric_change 在这种窗口上判「无数据」;切片侧却按 0 参与排名。两者对不上。
    """
    ledger = _scalar(con, f"SELECT SUM(amount) FROM read_parquet('{_parquet(MOVEMENT_TABLE)}')"
                          " WHERE account_id = ?", ["ACC_E001"])
    rows = _scalar(con, f"SELECT COUNT(*) FROM read_parquet('{_parquet(FACT_TABLE)}')"
                        " WHERE account_id = ? AND date_id >= ?", ["ACC_E001", "2026-02-20"])
    assert (ledger, rows) == (0, 0), "契约:流失后无行 + 累计变动归零"
    assert engine.query_metric("mrr", [], {"account_id": "ACC_E001"},
                               "2026-02-19", "2026-02-19")["total"] == 4_797_600.0
    assert engine.query_metric("mrr", [], {"account_id": "ACC_E001"},
                               "2026-02-20", FINAL_DAY)["total"] is None


# --- 2. 加法分解:Σ效应 ≡ 总变化(同原点)----------------------------------
@pytest.mark.parametrize("case_id", sorted(CASES))
def test_decompose_identity_has_zero_residual(engine, case_id):
    """mrr = 新签 + 扩张 + 降配 + 流失 的桥式分解:效应之和逐分等于总变化。"""
    base_end = CASES[case_id][0][1]
    cmp_end = CASES[case_id][1][1]
    result = engine.decompose("mrr", list(FACTORS), ORIGIN, base_end, ORIGIN, cmp_end)
    assert result["kind"] == "additive"
    effects = {effect["factor"]: effect["effect"] for effect in result["effects"]}
    assert set(effects) == set(FACTORS)
    residual = sum(effects.values()) - result["total_change"]
    assert abs(residual) <= 1e-6 * max(1.0, abs(result["total_change"])), (
        f"{case_id}: Σ效应 - 总变化 = {residual}(分);效应 = {effects}")
    # 分解的总变化 = 两个月末日的水平差(末日口径,不是求和)
    base_value = engine.query_metric("mrr", [], {}, base_end, base_end)["total"]
    cmp_value = engine.query_metric("mrr", [], {}, cmp_end, cmp_end)["total"]
    assert result["total_change"] == pytest.approx(cmp_value - base_value, rel=1e-9)


def test_decompose_rejects_disjoint_windows(engine):
    """caveat 第 2 条的可证伪形式:不同原点的两段窗口必须报错,不许静默给出带残差的数。

    直接拿 4 月对 5 月:基期总量是 4/30 的水平(25,238,100 分),而各因子的基期重建值
    只是 4 月一个月的流量(2,334,400 分)—— 恒等式不成立,必须抛 DecomposeError。
    """
    with pytest.raises(DecomposeError, match="重建值不符"):
        engine.decompose("mrr", list(FACTORS), "2026-04-01", "2026-04-30",
                         "2026-05-01", "2026-05-31")


# --- 3. 四个 case 都能被评估脚手架读出来 ------------------------------------
def test_all_four_cases_load():
    cases = {case.id: case for case in load_cases(str(DATASET))}
    assert set(cases) == set(CASES), "case 文件与本文档的窗口表必须一一对应"
    keys = set()
    for case_id, case in cases.items():
        base_window, cmp_window = CASES[case_id]
        assert case.root_cause_slice is not None
        assert case.root_cause_slice["level"] == case.required_depth == "account_id"
        assert case.contribution_range is not None and len(case.contribution_range) == 2
        assert len(case.must_not_claim) >= 3, "每个 case 都要声明错误的说法(空断言=静默失效)"
        assert case.tier in {"L2", "L3"} and case.category == "SaaS"
        # slice_occupancy.window = 埋点发生的那个窗口;它必须落在 case 自己讨论的两段窗口里
        # (s2 的埋点在 3 月那次扩容上,而 case 问的是 4 月为什么不再涨,故两者不同)
        window = [str(day) for day in case.raw["slice_occupancy"]["window"]]
        assert window in (list(base_window), list(cmp_window)), (
            f"{case_id} 的影响窗口应落在 case 的基期或对比期内(实际 {window})")
        keys.add(case.root_cause_slice["key"])
    assert len(keys) == 4, "四个 case 的根因切片必须互不相同"


# --- 4. 铁律一:快照 ≡ 变动账本 --------------------------------------------
def test_snapshot_equals_movement_ledger(con):
    """任意一天的「快照水平合计」恒等于「该日及以前的全部变动」—— 逐笔整数,差为 0。"""
    facts, movements = _parquet(FACT_TABLE), _parquet(MOVEMENT_TABLE)
    for day in RECON_DAYS:
        snapshot = _scalar(con, f"SELECT SUM(mrr_amount) FROM read_parquet('{facts}')"
                                " WHERE date_id = ?", [day])
        ledger = _scalar(con, f"SELECT SUM(amount) FROM read_parquet('{movements}')"
                              " WHERE date_id <= ?", [day])
        assert snapshot == ledger, f"{day}: 快照 {snapshot} ≠ 变动账本 {ledger}"
    assert _scalar(con, f"SELECT SUM(mrr_amount) FROM read_parquet('{_parquet(FACT_TABLE)}')"
                        " WHERE date_id = ?", [FINAL_DAY]) == 15_433_200   # = 154,332 元


def test_every_account_ledger_reconciles(con):
    """账户级同样精确:每个账户在数据末日的水平(无行 = 0)≡ 它全部变动之和。"""
    mismatched = con.execute(f"""
        WITH final AS (SELECT account_id AS a, SUM(mrr_amount) AS v
                       FROM read_parquet('{_parquet(FACT_TABLE)}')
                       WHERE date_id = '{FINAL_DAY}' GROUP BY 1),
             ledger AS (SELECT account_id AS a, SUM(amount) AS f
                        FROM read_parquet('{_parquet(MOVEMENT_TABLE)}') GROUP BY 1)
        SELECT COUNT(*) FROM read_parquet('{_parquet(ACCOUNT_TABLE)}') d
        LEFT JOIN final l ON d.account_id = l.a
        LEFT JOIN ledger g ON d.account_id = g.a
        WHERE COALESCE(l.v, 0) <> COALESCE(g.f, 0)""").fetchone()[0]
    accounts = con.execute(f"SELECT COUNT(*) FROM read_parquet('{_parquet(ACCOUNT_TABLE)}')"
                           ).fetchone()[0]
    assert (mismatched, accounts) == (0, 80)


# --- 5. 铁律二:S1–S4 的受影响账户集合两两不交(从数据现算)------------------
def test_planted_impact_sets_are_pairwise_disjoint(engine, con):
    """每个 case 的主因账户 = 该窗口里吃下埋点流量一半以上的账户;四组两两不交。"""
    cases = {case.id: case for case in load_cases(str(DATASET))}
    planted: dict[str, set[str]] = {}
    for case_id, (metric, start, end) in PLANTED.items():
        total = engine.query_metric(metric, [], {}, start, end)["total"]
        rows = engine.query_metric(metric, ["account_id"], {}, start, end)["rows"]
        planted[case_id] = _dominant_accounts(engine, metric, start, end)
        biggest = max(rows, key=lambda row: abs(row["value"]))
        assert biggest["account_id"] == cases[case_id].root_cause_slice["key"], (
            f"{case_id}: 窗口内流量最大的账户应是声明的主因({metric} {start}~{end} "
            f"全量 {total} 分)")
        assert len(planted[case_id]) == 1, "每个 case 的主因应当唯一"
    for left, right in itertools.combinations(sorted(planted), 2):
        assert not planted[left] & planted[right], (
            f"{left} 与 {right} 的受影响账户重叠:{planted[left] & planted[right]}")

    # 隔离也落在「套餐 × 月份」上:四个主因账户分属四个套餐,四个对比期窗口不重叠
    owners = sorted(account for accounts in planted.values() for account in accounts)
    marked = ", ".join("?" * len(owners))
    accounts_path = _parquet(ACCOUNT_TABLE)
    plans = dict(con.execute(f"SELECT account_id, plan FROM read_parquet('{accounts_path}')"
                             f" WHERE account_id IN ({marked})", owners).fetchall())
    assert len({plans[account] for account in owners}) == len(owners)
    windows = sorted(CASES[case_id][1] for case_id in CASES)
    for (_, end), (next_start, _) in zip(windows, windows[1:]):
        assert end < next_start, f"对比期窗口重叠:{end} 与 {next_start}"
