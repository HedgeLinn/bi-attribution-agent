"""ecommerce-demo 场景扩展(§5.3 埋点 E1/E2/E4/E5/E6)的回归测试。

两件事,各自独立:
① **新 case 可加载且形状正确** —— 5 个新埋点出现在评估集里,tier/切片/影响集合齐全
   (评估集是准确率的分母,坏 case 必须显式失败);
② **既有钉死数值一个没漂** —— 新埋点只允许落在与既有坑**不相交的(门店 × 时间窗)**上。
   这条是本模块的重点:造数脚本可重跑,但 2026-06(断崖 + 618 干扰项)、2025-12 /
   2026-01(c4 季节性)、STORE_S0001 / SKU_P0001 的数值是被 M3/M4 的测试逐值钉死的,
   扩展埋点**不得**让它们动一分。故这里不复述造数逻辑,只从引擎公开方法反向抽查。

全部离线:不碰 LLM,不需要 BI_API_KEY。真实引擎来自 tests/engine_fixtures.py
(lru_cache,与其它引擎集成测试共用同一份构造)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.datasets import resolve_dataset
from scripts.evaluate_agent import load_cases
from tests.engine_fixtures import real_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPO_ROOT / "datasets" / "ecommerce-demo"

# §5.3 新增的 5 个埋点(排序在 c1-c4 之后:文件名前缀 e > c,评估报告的分母顺序依赖它)
NEW_IDS = ["e1_pure_volume", "e2_pure_price", "e4_volume_price_reversal",
           "e5_store_mix_shift", "e6_hidden_discount"]

# 被既有测试逐值钉死的时间窗:新埋点的影响集合**必须**与它们不相交
PINNED_WINDOWS = [("2026-05-01", "2026-05-31"),   # 断崖基期 / 大促基期
                  ("2026-06-01", "2026-06-30"),   # 断崖 + 618 干扰项(核心验收)
                  ("2026-06-19", "2026-06-30"),   # c3 天数口径差窗口
                  ("2025-12-01", "2025-12-31"),   # c4 季节性
                  ("2026-01-01", "2026-01-31")]   # c4 季节性

TOL = 1e-9          # 引擎有 1e-16 级浮点抖动(DuckDB 并行聚合),只做相对容差比对


def _total(metric: str, filters: dict, start: str, end: str):
    """一个指标的窗口总计(空窗口 -> None,与引擎口径一致)。"""
    return real_engine().query_metric(metric, [], dict(filters), start, end)["total"]


# --- ① 新 case 可加载 --------------------------------------------------------
def test_new_cases_are_loadable_and_appended_after_the_original_four() -> None:
    """9 个 case(4 旧 + 5 新);新 case 都排在 c1-c4 之后,且 dataset.yaml 的分母对得上。"""
    cases = load_cases(str(DATASET))
    assert [c.id for c in cases] == ["c1_store_cliff", "c2_sku_delist", "c3_promo_noise",
                                     "c4_seasonal", *NEW_IDS]
    assert resolve_dataset("ecommerce-demo", datasets_dir=REPO_ROOT / "datasets").case_count \
        == len(cases)


def test_new_cases_declare_a_gradeable_root_cause_slice() -> None:
    """评分是 conclusion["根因"]["key"] 的严格相等 —— 五个新 case 必须是单键可判的。"""
    by_id = {c.id: c for c in load_cases(str(DATASET))}
    expected = {"e1_pure_volume": "STORE_S0002",
                "e2_pure_price": "STORE_S0003",
                "e4_volume_price_reversal": "STORE_S0004",
                "e5_store_mix_shift": "STORE_S0451",
                "e6_hidden_discount": "STORE_S0005"}
    for case_id, key in expected.items():
        case = by_id[case_id]
        assert case.tier == "L2", case_id            # 单坑时间窗 -> L2(不破坏 by_tier 的既有断言)
        assert case.category == "零售", case_id
        assert case.root_cause_slice["key"] == key, case_id
        assert case.root_cause_slice["dimension"] == "store", case_id
        assert case.root_cause_slice["level"] == case.required_depth == "store_id", case_id
        assert case.must_not_claim, case_id          # 漏读它「无错误断言」会静默变成空断言
        assert case.raw["slice_occupancy"]["keys"] == [key], case_id
        assert case.raw["overlap_with"] == [], case_id


def test_new_pits_do_not_land_in_the_pinned_windows() -> None:
    """新埋点的影响集合与既有钉死窗口零重叠 —— 这是「钉死数值不许漂」的第一道闸。"""
    for case in load_cases(str(DATASET)):
        if case.id not in NEW_IDS:
            continue
        occ = case.raw["slice_occupancy"]
        # YAML 里裸写的日期会被解析成 datetime.date,统一成 ISO 字符串再比
        start, end = (str(part) for part in occ["window"])
        assert start <= end, case.id
        for pinned_start, pinned_end in PINNED_WINDOWS:
            assert end < pinned_start or start > pinned_end, \
                f"{case.id} 的影响窗口 {start}~{end} 与钉死窗口 {pinned_start}~{pinned_end} 重叠"


# --- ② 既有钉死数值未漂 ------------------------------------------------------
@pytest.mark.parametrize("metric,filters,start,end,expected", [
    # 全量 GMV:M3-a / M4 的绊线(2026-06 环比 -20.95%)
    ("gmv", {}, "2026-05-01", "2026-05-31", 13692893.29),
    ("gmv", {}, "2026-06-01", "2026-06-30", 10824508.20),
    # c4 季节性
    ("gmv", {}, "2025-12-01", "2025-12-31", 15118256.14),
    ("gmv", {}, "2026-01-01", "2026-01-31", 11179380.00),
    # 埋点异常的标准答案:STORE_S0001 断崖
    ("gmv", {"store_id": "STORE_S0001"}, "2026-05-01", "2026-05-31", 1740234.97),
    ("gmv", {"store_id": "STORE_S0001"}, "2026-06-01", "2026-06-30", 741320.76),
    # c3 的 618 后回落窗口(天数口径差)
    ("gmv", {}, "2026-06-19", "2026-06-30", 4398881.68),
])
def test_pinned_values_are_untouched(metric, filters, start, end, expected) -> None:
    assert _total(metric, filters, start, end) == pytest.approx(expected, rel=TOL)


def test_pinned_store_cliff_change_rate_is_still_minus_57_percent() -> None:
    """断崖幅度本身(不只是两个绝对值):环比 -57.40% 是 anomaly/case 双方钉的量。"""
    base = _total("gmv", {"store_id": "STORE_S0001"}, "2026-05-01", "2026-05-31")
    cmp_ = _total("gmv", {"store_id": "STORE_S0001"}, "2026-06-01", "2026-06-30")
    assert cmp_ / base - 1 == pytest.approx(-0.5740, abs=5e-4)


# --- ③ 五个新坑真的存在(否则 case 是空头支票) -------------------------------
def test_e1_pure_volume_orders_halve_while_price_holds() -> None:
    """E1 纯量:订单数腰斩、客单价不动 —— 「量跌价稳」的分界线。"""
    assert _total("gmv", {"store_id": "STORE_S0002"}, "2025-07-01", "2025-07-31") \
        == pytest.approx(98171.23, rel=TOL)
    assert _total("orders_count", {"store_id": "STORE_S0002"},
                  "2025-07-01", "2025-07-31") == pytest.approx(180.0, rel=TOL)
    aov_06 = _total("aov", {"store_id": "STORE_S0002"}, "2025-06-01", "2025-06-30")
    aov_07 = _total("aov", {"store_id": "STORE_S0002"}, "2025-07-01", "2025-07-31")
    assert aov_07 / aov_06 - 1 == pytest.approx(-0.0393, abs=5e-4)   # 价稳(±5% 内)


def test_e2_pure_price_drops_while_order_count_holds() -> None:
    """E2 纯价:客单价跌、订单数不动 —— 与 E1 互为镜像。"""
    assert _total("gmv", {"store_id": "STORE_S0003"}, "2025-07-01", "2025-07-31") \
        == pytest.approx(150712.24, rel=TOL)
    aov_06 = _total("aov", {"store_id": "STORE_S0003"}, "2025-06-01", "2025-06-30")
    aov_07 = _total("aov", {"store_id": "STORE_S0003"}, "2025-07-01", "2025-07-31")
    assert aov_07 / aov_06 - 1 == pytest.approx(-0.2454, abs=5e-4)
    orders_06 = _total("orders_count", {"store_id": "STORE_S0003"}, "2025-06-01", "2025-06-30")
    orders_07 = _total("orders_count", {"store_id": "STORE_S0003"}, "2025-07-01", "2025-07-31")
    assert orders_07 / orders_06 - 1 == pytest.approx(-0.0027, abs=1e-2)   # 量稳(±1% 内)


def test_e4_volume_price_reversal_orders_up_price_down_gmv_flat() -> None:
    """E4 量价反向:订单 +52.66%、客单价 -33.99%、乘积使 GMV 几乎持平。"""
    orders_07 = _total("orders_count", {"store_id": "STORE_S0004"}, "2025-07-01", "2025-07-31")
    orders_08 = _total("orders_count", {"store_id": "STORE_S0004"}, "2025-08-01", "2025-08-31")
    assert (orders_07, orders_08) == (357.0, 545.0)
    assert orders_08 / orders_07 - 1 == pytest.approx(0.5266, abs=5e-4)
    aov_07 = _total("aov", {"store_id": "STORE_S0004"}, "2025-07-01", "2025-07-31")
    aov_08 = _total("aov", {"store_id": "STORE_S0004"}, "2025-08-01", "2025-08-31")
    assert aov_08 / aov_07 - 1 == pytest.approx(-0.3399, abs=5e-4)
    assert _total("gmv", {"store_id": "STORE_S0004"}, "2025-08-01", "2025-08-31") \
        == pytest.approx(197291.75, rel=TOL)


def test_e5_new_store_appears_from_nothing() -> None:
    """E5 权重结构变化:基准期该店**不存在**(None),对比期凭空贡献城市增量的 90%。"""
    assert _total("gmv", {"store_id": "STORE_S0451"}, "2025-03-01", "2025-03-31") is None
    assert _total("gmv", {"store_id": "STORE_S0451"}, "2025-04-01", "2025-04-30") \
        == pytest.approx(99619.92, rel=TOL)
    city_03 = _total("gmv", {"city": "合肥"}, "2025-03-01", "2025-03-31")
    city_04 = _total("gmv", {"city": "合肥"}, "2025-04-01", "2025-04-30")
    assert city_04 / city_03 - 1 == pytest.approx(0.2589, abs=5e-4)
    assert 99619.92 / (city_04 - city_03) == pytest.approx(0.9008, abs=5e-4)
    # 新店必须挂对城市:否则「按城市下钻」这条唯一可辩护的路径根本看不见它
    rows = real_engine().query_metric("gmv", ["store_id"], {"city": "合肥"},
                                      "2025-04-01", "2025-04-30")["rows"]
    by_key = {row["store_id"]: row["value"] for row in rows}
    assert by_key.get("STORE_S0451") == pytest.approx(99619.92, rel=TOL)
    assert all(value is not None for value in by_key.values())   # 存量 10 店也没被误伤成空行


def test_e6_discount_rate_doubles_while_gmv_stays_flat() -> None:
    """E6 隐性价降:唯一显形的指标是 discount_rate(gmv = SUM(amount) 不含折扣)。"""
    rate_07 = _total("discount_rate", {"store_id": "STORE_S0005"}, "2025-07-01", "2025-07-31")
    rate_08 = _total("discount_rate", {"store_id": "STORE_S0005"}, "2025-08-01", "2025-08-31")
    assert rate_07 == pytest.approx(0.017934866658652862, rel=TOL)
    assert rate_08 == pytest.approx(0.03256393564331851, rel=TOL)
    assert rate_08 / rate_07 - 1 == pytest.approx(0.8157, abs=1e-3)
    gmv_07 = _total("gmv", {"store_id": "STORE_S0005"}, "2025-07-01", "2025-07-31")
    gmv_08 = _total("gmv", {"store_id": "STORE_S0005"}, "2025-08-01", "2025-08-31")
    assert gmv_08 / gmv_07 - 1 == pytest.approx(0.0234, abs=5e-4)   # GMV 口径看不见
