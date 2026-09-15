"""AttributionEngine.detect_anomaly 的接线测试:真实数据集上的日历感知异常检测(§4.4)。

冻结的接线口径(方法 docstring 与 docs/ATTRIBUTION_CONTRACT.md):
    - 日序列 = [start − BASELINE_WEEKS_DEFAULT×7×2 天, end](估计窗口的 2 倍——
      促销日历剔除最多可吃掉一整个估计窗口的样本,2× 保证剔除后仍有等宽干净历史)
    - 基线由窗口之前的同星期几历史估计,促销日历内的样本不参与
    - cmp_start / cmp_end 已弃用:保留仅为签名兼容,结果与它们无关

真实数据的埋点:华东 -> 上海 STORE_S0001 自 2026-06 断崖下跌;语义层的日历把
2026-06-01..06-18 的 618 大促声明为「预期回落,不是异常」。

B 案例(618 干扰项,本里程碑的核心验收):大促刚结束的两周(06-19..06-30)相对历史
不应判为异常——自然回落是季节性的,不是业务问题。这个期望在 1× 回看口径下**不成立**
(剔掉 18 天促销样本后干净基线只剩 10 天大促前爬坡段,同星期几匹配做不成,退化 flat 且
基线虚高 -> 误报);把序列回看加倍后成立(-13.7%,低于 15% 阈值)。test_case_b_*
三个用例把「期望成立」「实际口径的数值」与「机理对照」分别钉死,口径再变会红。
"""

import datetime as dt
from pathlib import Path

import pytest
import yaml

from attribution.anomaly import BASELINE_WEEKS_DEFAULT, assess
from attribution.semantic import SemanticError
from tests.engine_fixtures import dataset_info, engine_with_layer, real_engine

# 冻结的返回键(docstring 声明「只增不改」):多一个少一个都算契约漂移
FROZEN_KEYS = {"base", "cmp", "change", "change_rate", "is_anomaly", "baseline_type",
               "is_expected", "anomaly_kind", "note", "baseline_mad", "robust_z"}

# 案例 A:整个大促月 —— 应判异常,但因命中促销日历而「属预期」
CASE_A = ("gmv", "2026-06-01", "2026-06-30", "2026-05-01", "2026-05-31")
# 案例 B:大促刚结束的两周 —— 任务书要求「不判异常」(当前口径下不成立)
CASE_B = ("gmv", "2026-06-19", "2026-06-30", "2026-05-19", "2026-05-31")

_TOLERANCE = 1e-3


def _engine_without_calendar(tmp_path):
    """真实地图的副本,只删掉 time.calendar:对照「这份地图没有促销日历知识」。"""
    raw = yaml.safe_load(Path(dataset_info().semantic_path).read_text(encoding="utf-8"))
    raw.pop("time", None)
    return engine_with_layer(tmp_path, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))


def test_case_a_flags_anomaly_and_marks_it_expected() -> None:
    """大促月:变化率超阈值(is_anomaly),但落在促销日历内 -> is_expected,note 点名。"""
    result = real_engine().detect_anomaly(*CASE_A)

    assert set(result) == FROZEN_KEYS | {"metric"}     # metric 是新加的,其余键全部保留
    assert result["metric"] == "gmv"
    assert result["is_anomaly"] is True
    assert result["is_expected"] is True               # 命中 618 大促
    assert result["baseline_type"] == "weekday_matched"  # 4 周回看够用,不做 flat 退化
    assert result["change_rate"] == pytest.approx(-0.1642, abs=_TOLERANCE)
    assert result["base"] > result["cmp"]              # 下滑
    assert "大促" in result["note"]


def test_cmp_window_parameters_are_ignored() -> None:
    """cmp_start / cmp_end 已弃用:换成任意窗口,结果逐值不变(基线不看它)。"""
    engine = real_engine()
    metric, start, end, _, _ = CASE_A
    assert engine.detect_anomaly(metric, start, end, "2026-05-01", "2026-05-31") == \
        engine.detect_anomaly(metric, start, end, "2019-01-01", "2019-01-31")


def test_unknown_metric_raises_semantic_error() -> None:
    """未知指标不许静默:取语义层时即抛 SemanticError。"""
    with pytest.raises(SemanticError):
        real_engine().detect_anomaly("查无此指标", *CASE_A[1:])


def test_timestamps_with_time_component_are_tolerated() -> None:
    """契约只承诺 YYYY-MM-DD,但带时间分量的写法与 anomaly 模块同口径容忍:
    归一化截掉时分后,结果与裸日期逐值相同(带时分的字符串不会把窗口首日挤掉)。"""
    engine = real_engine()
    metric, start, end, cmp_start, cmp_end = CASE_A
    plain = engine.detect_anomaly(metric, start, end, cmp_start, cmp_end)
    stamped = engine.detect_anomaly(metric, f"{start} 00:00:00", f"{end} 23:59:59",
                                    f"{cmp_start} 08:30:00", f"{cmp_end} 17:00:00")
    assert stamped == plain
    assert stamped["is_anomaly"] is True


def test_case_b_post_promo_slump_is_not_anomalous() -> None:
    """B 案例(核心验收):大促后两周的自然回落不判异常(2× 回看口径下成立)。"""
    result = real_engine().detect_anomaly(*CASE_B)

    assert result["is_anomaly"] is False           # 618 干扰项不再误报
    assert result["change_rate"] == pytest.approx(-0.1372, abs=_TOLERANCE)
    assert result["baseline_type"] == "flat"       # 同星期几样本不足,如实报 flat
    assert result["is_expected"] is False          # 窗口本身不在促销区间内
    assert "促销日历" in result["note"]            # 剔除了多少促销样本写进 note


def test_case_b_lookback_margin_is_what_makes_it_pass(tmp_path) -> None:
    """机理钉死:同一窗口同一数据,把回看缩回 1× 估计窗口,期望立即翻转为误报。

    这证明 B 案例靠「2× 回看余量」成立,不是靠取数或判定逻辑的其它变化;
    若有人把回看改回 1×,这里会红(与上一个用例互为绊线)。
    """
    engine = real_engine()
    lookback_1x = engine.source.daily_series(
        "gmv",
        (dt.date.fromisoformat(CASE_B[1]) - dt.timedelta(days=BASELINE_WEEKS_DEFAULT * 7)).isoformat(),
        CASE_B[2],
    )
    result = assess(lookback_1x, CASE_B[1], CASE_B[2], 0.15, engine.semantic.time_calendar)

    assert result["is_anomaly"] is True            # 1× 回看:误报(基线被促销剔除掏空)
    assert result["baseline_type"] == "flat"


def test_case_b_without_promo_calendar_is_not_anomalous(tmp_path) -> None:
    """机理对照:同一窗口、同一数据,不剔大促样本时也不判异常(基线含促销日)。

    两个对照合起来说明:B 案例的成立是「2× 回看余量 + 促销剔除」的正确相互作用。
    """
    result = _engine_without_calendar(tmp_path).detect_anomaly(*CASE_B)

    assert result["is_anomaly"] is False
    assert result["baseline_type"] == "weekday_matched"
    assert result["change_rate"] == pytest.approx(0.0084, abs=_TOLERANCE)
