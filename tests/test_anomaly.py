"""attribution.anomaly 的单元测试(docs/REUSE_DESIGN.md §4.4)。

全部用**内联构造的时间序列**,不依赖 datasets/ 下的真实数据,也不碰 DuckDB。
覆盖:促销日历命中 / 促销期不污染基线 / 同星期几基线真的生效 / 稳健统计 /
数据不足时不编基线 / 三种异常形态 / 冻结的返回契约。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from statistics import median

import pytest

from attribution.anomaly import BASELINE_WEEKS_DEFAULT, assess

# 测试日历:结构与语义层 time.calendar 一致(日历种类 -> 条目列表)。
# 促销窗口取**恰好两周**,让回看窗口里每个星期几都正好有一半落在促销期内,
# 「剔除促销日」的效应才可精确预期(2 个促销样本 + 2 个日常样本)。
PROMO_START = "2026-06-01"
PROMO_END = "2026-06-14"
PROMO_NAME = "618 大促"
CALENDAR = {"promos": [{"name": PROMO_NAME, "range": [PROMO_START, PROMO_END],
                        "note": "大促后自然回落属预期,不是异常"}]}

# 2026-06-01 是周一,故 05-04 ~ 05-31 恰好是 4 个整周,可作干净的基线期
CLEAN_START = "2026-05-04"
CLEAN_DAYS = 28
PROMO_DAYS = 14
THRESHOLD = 0.15

# 冻结的返回键(engine 会直接透传给上层,少一个都是破坏契约)
FROZEN_KEYS = ("base", "cmp", "change", "change_rate", "is_anomaly",
               "baseline_type", "is_expected", "anomaly_kind", "note")


def build_series(start: str, days: int, value: float | Callable[[int, dt.date], float]
                 ) -> list[tuple[str, float]]:
    """从 start 起连续 days 天的日序列;value 为常量或 (第几天, 日期) -> 值。"""
    origin = dt.date.fromisoformat(start)
    rows: list[tuple[str, float]] = []
    for offset in range(days):
        day = origin + dt.timedelta(days=offset)
        rows.append((day.isoformat(), float(value(offset, day) if callable(value) else value)))
    return rows


def clean_baseline(value: float = 100.0) -> list[tuple[str, float]]:
    """4 个整周的干净基线期(05-04 ~ 05-31),默认值 100。"""
    return build_series(CLEAN_START, CLEAN_DAYS, value)


# ---------------------------------------------------------------------------
# ① 促销日历:命中即预期内,且 note 点名是哪个促销期
# ---------------------------------------------------------------------------
def test_promo_window_is_expected_and_names_the_promo() -> None:
    series = clean_baseline() + build_series(PROMO_START, PROMO_DAYS, 200.0)
    result = assess(series, PROMO_START, PROMO_END, THRESHOLD, CALENDAR)

    assert result["base"] == 100.0        # 基线只由干净期决定
    assert result["cmp"] == 200.0
    assert result["change_rate"] == 1.0
    assert result["is_anomaly"] is True   # 变化率确实超阈值
    assert result["is_expected"] is True  # 但落在促销日历内 -> 属预期
    assert result["anomaly_kind"] == "pulse"
    assert PROMO_NAME in result["note"]   # 说明里点名了是哪个促销期
    assert "预期脉冲" in result["note"]


def test_calendar_is_traversed_generically_over_every_kind() -> None:
    """日历种类名不写死:换种类名照样识别,多种日历同时命中时全部点名。"""
    series = clean_baseline() + build_series(PROMO_START, PROMO_DAYS, 200.0)
    holidays = {"holidays": [{"name": "长假", "range": [PROMO_START, PROMO_END]}]}
    both = {"holidays": holidays["holidays"], "promos": CALENDAR["promos"]}

    assert assess(series, PROMO_START, PROMO_END, THRESHOLD, holidays)["is_expected"] is True
    assert assess(series, PROMO_START, PROMO_END, THRESHOLD, {})["is_expected"] is False

    result = assess(series, PROMO_START, PROMO_END, THRESHOLD, both)
    assert "长假" in result["note"] and PROMO_NAME in result["note"]


def test_promo_days_do_not_pollute_baseline() -> None:
    """拿大促当基线去比,基线本身就是被污染的:剔除促销日后误报消失。"""
    series = (clean_baseline() + build_series(PROMO_START, PROMO_DAYS, 200.0)
              + build_series("2026-06-15", 4, 100.0))   # 大促后自然回落
    kept = assess(series, "2026-06-15", "2026-06-18", THRESHOLD, CALENDAR)
    naive = assess(series, "2026-06-15", "2026-06-18", THRESHOLD, None)

    assert kept["base"] == 100.0          # 促销日剔除后,基线回到日常水平
    assert kept["change_rate"] == 0.0
    assert kept["is_anomaly"] is False    # 自然回落不再是异常
    assert "剔除" in kept["note"]

    assert naive["base"] == 150.0         # 不剔除时被促销日抬高(2 促销 + 2 日常)
    assert naive["change_rate"] == -0.3333
    assert naive["is_anomaly"] is True    # -> 把自然回落误报成异常


# ---------------------------------------------------------------------------
# ② 同星期几基线:周内效应数据上,它与整段中位数基线给出相反判定
# ---------------------------------------------------------------------------
def test_weekday_matched_baseline_differs_from_flat_median() -> None:
    series = build_series("2026-05-04", 42,
                          lambda offset, day: 100.0 if day.weekday() >= 5 else 200.0)
    cmp_start, cmp_end = "2026-06-13", "2026-06-14"     # 周六、周日
    result = assess(series, cmp_start, cmp_end, THRESHOLD)

    flat_baseline = median([value for stamp, value in series if stamp < cmp_start])
    flat_rate = (result["cmp"] - flat_baseline) / flat_baseline

    assert result["baseline_type"] == "weekday_matched"
    assert result["base"] == 100.0          # 周末只跟周末比
    assert flat_baseline == 200.0           # 整段中位数被工作日(200)拉高
    assert result["change_rate"] == 0.0
    assert result["is_anomaly"] is False    # 同星期几基线:不异常
    assert flat_rate == -0.5                # 整段中位数基线:会误报成 -50%
    assert abs(flat_rate) >= THRESHOLD


# ---------------------------------------------------------------------------
# ③ 稳健统计:中位数 + MAD 对离群点不敏感
# ---------------------------------------------------------------------------
def test_single_outlier_does_not_move_median_baseline() -> None:
    window = build_series("2026-06-01", 7, 90.0)
    outlier = [(stamp, 100000.0 if stamp == "2026-05-13" else value)
               for stamp, value in clean_baseline()]
    clean = assess(clean_baseline() + window, "2026-06-01", "2026-06-07", THRESHOLD)
    dirty = assess(outlier + window, "2026-06-01", "2026-06-07", THRESHOLD)

    assert (clean["base"], clean["baseline_mad"]) == (100.0, 0.0)
    assert (dirty["base"], dirty["baseline_mad"]) == (100.0, 0.0)   # 单点离群撼不动中位数
    assert clean["change_rate"] == dirty["change_rate"] == -0.1


def test_mad_is_reported_and_scales_the_robust_z() -> None:
    """基线带 ±1 的正常波动时,MAD 进入返回值,robust_z 以 MAD 为单位衡量偏离。"""
    series = (build_series(CLEAN_START, CLEAN_DAYS, lambda offset, day: 99.0 if offset % 2 else 101.0)
              + build_series("2026-06-01", 6, 60.0))
    result = assess(series, "2026-06-01", "2026-06-06", THRESHOLD)

    assert result["base"] == 100.0
    assert result["baseline_mad"] == 1.0
    assert result["change"] == -40.0
    assert result["robust_z"] < -20.0     # 相对 MAD 离得极远


# ---------------------------------------------------------------------------
# ④ 数据不足:如实说明,不编基线
# ---------------------------------------------------------------------------
def test_no_data_in_window_does_not_invent_baseline() -> None:
    series = clean_baseline() + build_series("2026-07-01", 5, 100.0)
    result = assess(series, "2026-06-10", "2026-06-20", THRESHOLD)

    assert result["base"] is None and result["cmp"] is None
    assert result["change"] is None and result["change_rate"] is None
    assert result["is_anomaly"] is False
    assert result["baseline_type"] == "none"
    assert result["anomaly_kind"] is None
    assert "数据不足" in result["note"]


def test_empty_series_does_not_invent_baseline() -> None:
    result = assess([], "2026-06-10", "2026-06-20", THRESHOLD)
    assert result["base"] is None
    assert result["is_anomaly"] is False
    assert "数据不足" in result["note"]


def test_baseline_fully_covered_by_calendar_does_not_invent_baseline() -> None:
    """基线期整段落在日历内时同样不编基线——没有可比样本就是没有。"""
    series = (clean_baseline() + build_series(PROMO_START, PROMO_DAYS, 200.0)
              + build_series("2026-06-15", 3, 100.0))
    long_calendar = {"promos": [{"name": "超长促销", "range": [CLEAN_START, PROMO_END]}]}
    result = assess(series, "2026-06-15", "2026-06-17", THRESHOLD, long_calendar)

    assert result["base"] is None
    assert result["baseline_type"] == "none"
    assert result["is_anomaly"] is False


# ---------------------------------------------------------------------------
# ⑤ 退化:同星期几做不成时如实报告 flat
# ---------------------------------------------------------------------------
def test_insufficient_history_degrades_to_flat_and_says_so() -> None:
    series = build_series("2026-05-28", 4, 100.0) + build_series(PROMO_START, 3, 60.0)
    result = assess(series, PROMO_START, "2026-06-03", THRESHOLD)

    assert result["baseline_type"] == "flat"     # 历史不满 4 周,如实退化
    assert result["base"] == 100.0
    assert "退化" in result["note"] and "flat" in result["note"]
    assert result["is_anomaly"] is True


def test_weekday_with_too_few_samples_degrades_to_flat() -> None:
    """对比窗口是周末,但回看窗口里只有工作日数据 -> 匹配样本为 0,退化。"""
    weekdays_only = [row for row in clean_baseline(200.0)
                     if dt.date.fromisoformat(row[0]).weekday() < 5]
    series = weekdays_only + build_series("2026-06-06", 2, 200.0)   # 周六、周日
    result = assess(series, "2026-06-06", "2026-06-07", THRESHOLD)

    assert result["baseline_type"] == "flat"
    assert result["base"] == 200.0
    assert result["is_anomaly"] is False


# ---------------------------------------------------------------------------
# ⑥ anomaly_kind 三种形态
# ---------------------------------------------------------------------------
def test_anomaly_kind_level_shift_for_stable_new_platform() -> None:
    series = (build_series(CLEAN_START, CLEAN_DAYS, lambda offset, day: 99.0 if offset % 2 else 101.0)
              + build_series("2026-06-01", 6, 60.0))
    result = assess(series, "2026-06-01", "2026-06-06", THRESHOLD)

    assert result["is_anomaly"] is True
    assert result["anomaly_kind"] == "level_shift"
    assert "台阶" in result["note"]


def test_anomaly_kind_trend_change_for_drift_inside_window() -> None:
    series = clean_baseline() + build_series("2026-06-01", 6,
                                             lambda offset, day: 100.0 - 8.0 * offset)
    result = assess(series, "2026-06-01", "2026-06-06", THRESHOLD)

    assert result["base"] == 100.0
    assert result["cmp"] == 80.0
    assert result["change_rate"] == -0.2
    assert result["anomaly_kind"] == "trend_change"
    assert "趋势" in result["note"]


def test_anomaly_kind_pulse_for_calendar_window_and_none_without_anomaly() -> None:
    promo = clean_baseline() + build_series(PROMO_START, PROMO_DAYS, 200.0)
    assert assess(promo, PROMO_START, PROMO_END, THRESHOLD, CALENDAR)["anomaly_kind"] == "pulse"

    quiet = clean_baseline() + build_series("2026-06-01", 7, 101.0)
    assert assess(quiet, "2026-06-01", "2026-06-07", THRESHOLD)["anomaly_kind"] is None


# ---------------------------------------------------------------------------
# ⑦ 冻结契约与边界
# ---------------------------------------------------------------------------
def test_result_carries_every_frozen_key() -> None:
    series = clean_baseline() + build_series(PROMO_START, PROMO_DAYS, 200.0)
    for result in (assess(series, PROMO_START, PROMO_END, THRESHOLD, CALENDAR),
                   assess(series, PROMO_START, PROMO_END, THRESHOLD),
                   assess(series, "2026-06-10", "2026-06-20", THRESHOLD)):
        assert set(FROZEN_KEYS) <= set(result)


def test_change_and_rate_follow_the_contract_invariant() -> None:
    """§7 的通用不变量:change == cmp - base,change_rate == (cmp - base) / base。"""
    series = clean_baseline() + build_series("2026-06-01", 7, 130.0)
    result = assess(series, "2026-06-01", "2026-06-07", THRESHOLD)

    assert result["change"] == result["cmp"] - result["base"]
    assert result["change_rate"] == round(result["change"] / result["base"], 4)
    assert result["is_anomaly"] == (abs(result["change_rate"]) >= THRESHOLD)
    assert result["baseline_type"] == "weekday_matched"


def test_zero_baseline_has_no_change_rate() -> None:
    series = build_series(CLEAN_START, CLEAN_DAYS, 0.0) + build_series("2026-06-01", 7, 5.0)
    result = assess(series, "2026-06-01", "2026-06-07", THRESHOLD)

    assert result["base"] == 0.0
    assert result["change"] == 5.0
    assert result["change_rate"] is None          # 基线为 0 时变化率没有定义
    assert result["is_anomaly"] is False
    assert "未定义(基线为 0)" in result["note"]   # 说明里如实写明变化率没有定义


def test_missing_values_are_dropped_not_treated_as_zero() -> None:
    """缺数是缺数,不是 0(§5.4 的 C4):窗口里两天没数据,中位数不该被拉低。"""
    window = [("2026-06-01", 90.0), ("2026-06-02", None),
              ("2026-06-03", 50.0), ("2026-06-04", 90.0)]
    result = assess(clean_baseline() + window, "2026-06-01", "2026-06-04", THRESHOLD)

    assert result["cmp"] == 90.0    # 若把缺失当 0,中位数会变成 70
    assert result["change_rate"] is not None


def test_malformed_stamp_raises_instead_of_silently_dropping() -> None:
    with pytest.raises(ValueError):
        assess([("2026/06/01", 1.0)], "2026-06-01", "2026-06-07", THRESHOLD)


def test_reversed_window_raises() -> None:
    with pytest.raises(ValueError):
        assess(clean_baseline(), "2026-06-07", "2026-06-01", THRESHOLD)


def test_default_baseline_weeks_is_used_when_omitted() -> None:
    """不传 baseline_weeks 时用默认常量;显式传同样的值结果一致。"""
    series = clean_baseline() + build_series(PROMO_START, PROMO_DAYS, 200.0)
    implicit = assess(series, PROMO_START, PROMO_END, THRESHOLD, CALENDAR)
    explicit = assess(series, PROMO_START, PROMO_END, THRESHOLD, CALENDAR,
                      baseline_weeks=BASELINE_WEEKS_DEFAULT)
    assert implicit == explicit
