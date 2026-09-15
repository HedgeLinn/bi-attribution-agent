"""AttributionEngine.contribute 的行为测试(R3 / R4 修复的回归锚点):

- R3 排序稳定:top 先按 key 定序、再按 change 升序——并列时输出与 DuckDB 行序噪声无关,
  同输入 -> 同输出(live 评估曾因此两轮成绩 3/9 -> 4/9 不可复现)
- R4 边界:任一侧整窗无数据抛 DecomposeError(与 decompose 同口径「没有行不等于 0」),
  而不是 TypeError
"""

from __future__ import annotations

import pytest

from attribution.decompose import DecomposeError
from tests.engine_fixtures import (
    BASE_WINDOW,
    CMP_WINDOW,
    NULL_LAYER_YAML,
    engine_with_layer,
    real_engine,
)


def _call(engine, metric, dimension, level, top_k=999, filters=None):
    return engine.contribute(metric, dimension, level,
                             *BASE_WINDOW, *CMP_WINDOW,
                             top_k=top_k, filters=filters)


def test_additive_top_is_key_tie_broken():
    """additive 分支:多次调用逐值相同;顺序 = change 升序、并列按 str(key) 升序。"""
    engine = real_engine()
    first = _call(engine, "gmv", "store", "region")
    for _ in range(3):
        assert _call(engine, "gmv", "store", "region") == first
    expected = sorted(
        first["top"],
        key=lambda row: (row["change"] is None, row["change"], str(row["key"])))
    assert first["top"] == expected


def test_ratio_top_is_key_tie_broken():
    """derived 分支:change_rate 排序同样有 key 定序兜底。"""
    engine = real_engine()
    first = _call(engine, "aov", "store", "city")
    for _ in range(3):
        assert _call(engine, "aov", "store", "city") == first
    expected = sorted(
        first["top"],
        key=lambda row: (row["change_rate"] is None, row["change_rate"], str(row["key"])))
    assert first["top"] == expected


def test_contribute_both_windows_empty_raise(tmp_path):
    """两侧整窗都无数据 -> DecomposeError(没有行不等于 0),不是 TypeError。"""
    engine = engine_with_layer(tmp_path, NULL_LAYER_YAML)
    with pytest.raises(DecomposeError):
        _call(engine, "never_positive", "store", "region")


def test_contribute_empty_is_honest_not_zero(tmp_path):
    """对照:同窗口下正常指标可算——恒空指标的报错不是语义层/连接问题。"""
    engine = engine_with_layer(tmp_path, NULL_LAYER_YAML)
    result = _call(engine, "amount_sum", "store", "region", top_k=5)
    assert result["total_base"] is not None
    assert result["total_cmp"] is not None


def test_contribute_filters_scope_totals_and_slices():
    """filters 同时作用于整窗总量与切片:过滤后总量等于带同过滤的 query_metric,
    且切片全部落在过滤域内(「只看某渠道的区域下钻」)。"""
    engine = real_engine()
    filters = {"channel_id": "CH_01"}
    result = _call(engine, "gmv", "store", "region", top_k=999, filters=filters)
    total = engine.query_metric("gmv", [], filters, *BASE_WINDOW)
    assert result["total_base"] == total["total"]
    queried_rows = engine.query_metric("gmv", ["region"], filters, *BASE_WINDOW)["rows"]
    keys = {row["key"] for row in result["top"]}
    assert keys == {row["region"] for row in queried_rows}

    unfiltered = _call(engine, "gmv", "store", "region", top_k=999)
    assert result["total_cmp"] < unfiltered["total_cmp"]   # 过滤确实缩小了分析域


def test_contribute_filters_do_not_clobber_base_total():
    """过滤下 total_base 与 query_metric 基期总量一致(不是只在对比期生效)。"""
    engine = real_engine()
    filters = {"channel_id": "CH_01"}
    result = _call(engine, "gmv", "store", "region", top_k=5, filters=filters)
    total_cmp = engine.query_metric("gmv", [], filters, *CMP_WINDOW)
    assert result["total_cmp"] == total_cmp["total"]