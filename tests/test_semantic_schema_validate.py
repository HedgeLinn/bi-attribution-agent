"""语义层「校验、可达性与编译」契约测试(REUSE_DESIGN §3.2 / §3.5 / §3.6④)。

覆盖三块:
    - 自洽性校验(§3.5):每种非法情形都要报出对应问题,一次报全不吞掉
    - 可达性校验(§3.6④,必须传 columns):join 不上、层级字段缺失、孤立维度
    - 编译结果:additive / derived 的聚合表达式、依赖声明一致性

加载与结构级失败见 test_semantic_schema_load.py;共享夹具见 semantic_fixtures.py。
与 attribution.expression(并行开发的编译器)相关的用例由 needs_compiler 装饰:
编译器未落地时按 xfail(reason="等待 expression 编译器落地", strict=False) 挂起,
落地后正常执行、正常报错,不做屏蔽。
"""

import pytest

from attribution.semantic import Semantic, SemanticError
from tests.semantic_fixtures import (
    BASE_COLUMNS,
    BASE_YAML,
    MRR_BLOCK,
    load_problems,
    needs_compiler,
    write_layer,
)


# ----------------------------------------------------------------------
# 自洽性校验:每种非法情形都要报出对应问题
# ----------------------------------------------------------------------
_RATIO_METRIC = """  conv:
    label: 转化率
    expression: SUM(converted) / SUM(visits)
    type: ratio
    depends_on: [converted, visits]
"""

_INVALID_CASES = [
    ("semi_additive 未声明 time_aggregation",
     BASE_YAML.replace(MRR_BLOCK, MRR_BLOCK.replace("    time_aggregation: last\n", "")),
     "必须显式声明 time_aggregation"),
    ("semi_additive 声明为 sum",
     BASE_YAML.replace("    time_aggregation: last\n", "    time_aggregation: sum\n"),
     "不得为 sum"),
    ("ratio 缺 two_stage",
     BASE_YAML.replace("dimensions:", _RATIO_METRIC + "dimensions:"),
     "two_stage"),
    ("hierarchy 为空",
     BASE_YAML.replace("    hierarchy: [year, dt]", "    hierarchy: []"),
     "hierarchy 为空"),
    ("hierarchy 有重复项",
     BASE_YAML.replace("    hierarchy: [country, region_id]",
                       "    hierarchy: [country, region_id, region_id]"),
     "重复字段"),
    ("key 不在 hierarchy 中",
     BASE_YAML.replace("    key: region_id", "    key: region_name"),
     "不在其 hierarchy 中"),
    ("维度缺 key/table",
     BASE_YAML.replace("    key: region_id\n", ""),
     "缺少 key 或 table"),
    ("date_field 为空",
     BASE_YAML.replace("date_field: dt\n", 'date_field: ""\n'),
     "date_field 为空"),
]


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [pytest.param(case[1], case[2], id=case[0]) for case in _INVALID_CASES],
)
def test_validate_reports_problem(tmp_path, yaml_text, expected):
    problems = load_problems(tmp_path, yaml_text)
    assert any(expected in p for p in problems), f"期望包含 {expected},实际 {problems}"


def test_assert_valid_collects_all_problems(tmp_path):
    text = BASE_YAML.replace("    hierarchy: [year, dt]", "    hierarchy: []")
    text = text.replace(MRR_BLOCK, MRR_BLOCK.replace("    time_aggregation: last\n", ""))
    text = text.replace("dimensions:", _RATIO_METRIC + "dimensions:")
    with pytest.raises(SemanticError) as err:
        Semantic.load(write_layer(tmp_path, text)).assert_valid()
    message = str(err.value)
    for token in ("hierarchy 为空", "time_aggregation", "two_stage"):
        assert token in message


# ----------------------------------------------------------------------
# 可达性校验(必须传 columns)
# ----------------------------------------------------------------------
_ORPHAN_DIM = """  ghost:
    label: 幽灵维度
    table: dim_ghost
    key: ghost_id
    hierarchy: [ghost_a, ghost_b]
"""

_REACH_CASES = [
    ("维度 key 不在事实表",
     BASE_YAML,
     {**BASE_COLUMNS, "fact_sales": BASE_COLUMNS["fact_sales"] - {"region_id"}},
     "无法 join"),
    ("层级字段不在维度表",
     BASE_YAML,
     {**BASE_COLUMNS, "dim_region": BASE_COLUMNS["dim_region"] - {"country"}},
     "不在维度表 dim_region"),
    ("date_field 不在事实表",
     BASE_YAML,
     {**BASE_COLUMNS, "fact_sales": BASE_COLUMNS["fact_sales"] - {"dt"}},
     "date_field"),
    ("孤立维度",
     BASE_YAML + _ORPHAN_DIM,
     {**BASE_COLUMNS,
      "fact_sales": BASE_COLUMNS["fact_sales"] | {"ghost_id"},
      "dim_ghost": {"ghost_id", "other"}},
     "孤立维度"),
]


@pytest.mark.parametrize(
    ("yaml_text", "columns", "expected"),
    [pytest.param(case[1], case[2], case[3], id=case[0]) for case in _REACH_CASES],
)
def test_reachability_reports_problem(tmp_path, yaml_text, columns, expected):
    sem = Semantic.load(write_layer(tmp_path, yaml_text), columns)
    problems = sem.check_reachability()
    assert any(expected in p for p in problems), f"期望包含 {expected},实际 {problems}"


def test_reachability_skips_unknown_tables(tmp_path):
    """维度表列信息未知:跳过该维度,不误报。"""
    sem = Semantic.load(write_layer(tmp_path, BASE_YAML), {"fact_sales": BASE_COLUMNS["fact_sales"]})
    assert sem.check_reachability() == []


# ----------------------------------------------------------------------
# 编译结果:additive / derived
# ----------------------------------------------------------------------
@needs_compiler
def test_agg_expr_additive_and_derived(tmp_path):
    sem = Semantic.load(write_layer(tmp_path, BASE_YAML), BASE_COLUMNS)
    additive = sem.agg_expr("revenue")
    assert "SUM" in additive.upper() and "amount" in additive
    derived = sem.agg_expr("aov")
    assert "amount" in derived and "orders_cnt" not in derived   # derived 已展开为底层聚合
    aliased = sem.agg_expr("revenue", alias="f")
    assert "f.amount" in aliased and "o.amount" not in aliased


def test_agg_expr_without_columns_raises(tmp_path):
    """未提供 columns 时无法编译:明确报错,而不是给出半成品。"""
    sem = Semantic.load(write_layer(tmp_path, BASE_YAML))
    with pytest.raises(SemanticError, match="columns"):
        sem.agg_expr("revenue")


@needs_compiler
def test_load_fails_on_uncompilable_expression(tmp_path):
    text = BASE_YAML.replace("SUM(amount)", "SUM(no_such_column)")
    with pytest.raises(SemanticError, match="无法编译"):
        Semantic.load(write_layer(tmp_path, text), BASE_COLUMNS)


@needs_compiler
def test_depends_on_mismatch_reported(tmp_path):
    text = BASE_YAML.replace("    depends_on: [amount]", "    depends_on: [mrr_amount]")
    problems = load_problems(tmp_path, text)
    assert any("不一致" in p for p in problems), problems


def test_columns_are_normalized(tmp_path):
    """columns 传各种可迭代集合都能工作(内部归一化为 frozenset)。"""
    sem = Semantic.load(write_layer(tmp_path, BASE_YAML),
                        {t: tuple(c) for t, c in BASE_COLUMNS.items()})
    sem.assert_valid()
