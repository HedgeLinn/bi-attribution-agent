"""SqlSource.daily_series 的数据访问测试(真实数据集上的 DuckDB 取数)。

冻结的契约(方法 docstring):
    - 按**事实表日期字段**分组直接聚合,不 join 维度表
    - 缺失日期不出现在结果里(没有行不等于 0,§5.4 的 C4)
    - 返回 [(日期 'YYYY-MM-DD', 值)],按日期升序

它是 detect_anomaly 的唯一数据来源,所以「日序列与逐日切片口径一致」必须被钉死:
两处对不上,基线就会拿另一套数去比。
"""

import pytest

from tests.engine_fixtures import NULL_LAYER_YAML, engine_with_layer, real_engine

_WINDOW = ("2026-06-01", "2026-06-30")
_DAYS_IN_JUNE = 30


def test_daily_series_is_ascending_and_bounded() -> None:
    """升序、无重复、逐日落在窗口内:异常检测的基线样本要能按日期切片。"""
    series = real_engine().source.daily_series("gmv", *_WINDOW)

    stamps = [stamp for stamp, _ in series]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)
    assert stamps[0] == _WINDOW[0] and stamps[-1] == _WINDOW[1]
    assert len(series) == _DAYS_IN_JUNE          # 每天都有成交,30 天一个不少
    assert all(isinstance(stamp, str) and isinstance(value, float)
               for stamp, value in series)


def test_daily_series_matches_query_metric_daily_slices() -> None:
    """日序列与 query_metric 的逐日切片逐值相同:同一个指标的日口径只有一处。"""
    engine = real_engine()
    series = engine.source.daily_series("gmv", *_WINDOW)
    rows = engine.query_metric("gmv", ["date_id"], {}, *_WINDOW)["rows"]

    assert {str(row["date_id"]): row["value"] for row in rows} == dict(series)
    assert len(rows) == _DAYS_IN_JUNE


def test_daily_series_window_without_rows_is_empty() -> None:
    """窗口内没有任何行 -> 空序列:不许拿 0 冒充「那天没有成交」。"""
    assert real_engine().source.daily_series("gmv", "2019-01-01", "2019-01-31") == []


def test_daily_series_keeps_none_when_aggregate_is_null(tmp_path) -> None:
    """有行但整组没有有效值:聚合为 NULL -> 保持 None,不编成 0(§5.4 的 C4)。

    临时地图里的 never_positive 指标在真实数据上每天都不命中任何行(amount 全为正),
    于是每天的聚合结果都是 NULL;同一个引擎里正常指标的日序列则必须是数值。
    """
    engine = engine_with_layer(tmp_path, NULL_LAYER_YAML)
    series = engine.source.daily_series("never_positive", *_WINDOW)

    assert len(series) == _DAYS_IN_JUNE          # 日期仍然齐,不是被丢掉
    assert all(value is None for _, value in series)
    assert all(value is not None
               for _, value in engine.source.daily_series("amount_sum", *_WINDOW))


@pytest.mark.parametrize("metric", ["gmv", "orders_count"])
def test_daily_series_is_single_window_aggregation(metric: str) -> None:
    """窗口只是过滤条件:同一指标按窗口切出来的日序列互不重叠、拼起来对得上总量。"""
    engine = real_engine()
    first = engine.source.daily_series(metric, "2026-06-01", "2026-06-15")
    second = engine.source.daily_series(metric, "2026-06-16", "2026-06-30")
    whole = engine.source.daily_series(metric, *_WINDOW)

    assert dict(first) | dict(second) == dict(whole)
    assert len(first) + len(second) == len(whole) == _DAYS_IN_JUNE
    # 两半的标量之和 = 整月的标量(逐日聚合与整窗聚合同口径)
    assert (sum(value for _, value in first) + sum(value for _, value in second)
            == pytest.approx(engine.query_metric(metric, [], {}, *_WINDOW)["total"],
                             rel=1e-9))
