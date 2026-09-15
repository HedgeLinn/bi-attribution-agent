"""语义层「加载与结构」契约测试(REUSE_DESIGN §3.2 / §3.5 / §3.6④)。

覆盖三块:
    - 合法语义层能加载,访问器(metric / dimension / field_to_dimension / 渲染)行为正确
    - 结构级错误在 load 阶段 fail-fast:缺 dataset_version、缺 date_field、
      不支持的 schema_version、顶层不是 mapping、依赖成环
    - 传/不传 columns 两条路径的差别(不传就不做依赖列信息的校验)

校验失败案例与可达性见 test_semantic_schema_validate.py;共享夹具见 semantic_fixtures.py。
"""

import pytest

from attribution.semantic import TYPE_DERIVED, Semantic, SemanticError
from tests.semantic_fixtures import BASE_COLUMNS, BASE_YAML, write_layer


# ----------------------------------------------------------------------
# 合法语义层
# ----------------------------------------------------------------------
def test_valid_layer_loads(tmp_path):
    sem = Semantic.load(write_layer(tmp_path, BASE_YAML))
    assert sem.schema_version == "2.0"
    assert sem.dataset == "unit-test-shop"
    assert sem.dataset_version == "1.2.3"
    assert sem.fact_table == "fact_sales"
    assert sem.date_field == "dt"
    assert set(sem.metrics) == {"revenue", "orders_cnt", "aov", "mrr"}
    assert sem.metric("aov").type == TYPE_DERIVED
    assert sem.dimension("region").hierarchy == ("country", "region_id")
    assert sem.all_levels()["day"] == ("year", "dt")
    assert sem.validate() == []


def test_valid_layer_with_columns_passes(tmp_path):
    """传了 columns:编译 + 可达性都能过。"""
    sem = Semantic.load(write_layer(tmp_path, BASE_YAML), BASE_COLUMNS)
    assert sem.validate() == []
    assert sem.check_reachability() == []
    sem.assert_valid()


def test_none_columns_skips_column_dependent_checks(tmp_path):
    """columns=None:不报错,也不做可达性校验。"""
    sem = Semantic.load(write_layer(tmp_path, BASE_YAML))
    assert sem.validate() == []
    assert sem.check_reachability() == []


def test_field_to_dimension(tmp_path):
    sem = Semantic.load(write_layer(tmp_path, BASE_YAML))
    assert sem.field_to_dimension("country") == "region"
    assert sem.field_to_dimension("year") == "day"
    assert sem.field_to_dimension("amount") is None


def test_render_overview_lists_metrics_and_dimensions(tmp_path):
    text = Semantic.load(write_layer(tmp_path, BASE_YAML)).render_overview()
    for token in ("revenue", "营收", "SUM(amount)", "aov", "region", "country -> region_id"):
        assert token in text


def test_unknown_metric_and_dimension_raise(tmp_path):
    sem = Semantic.load(write_layer(tmp_path, BASE_YAML))
    with pytest.raises(SemanticError):
        sem.metric("nope")
    with pytest.raises(SemanticError):
        sem.dimension("nope")
    with pytest.raises(SemanticError):
        sem.agg_expr("nope")


# ----------------------------------------------------------------------
# 结构级失败:load 阶段就应拒绝
# ----------------------------------------------------------------------
def test_dataset_version_fail_fast(tmp_path):
    text = BASE_YAML.replace('dataset_version: "1.2.3"\n', "")
    sem = Semantic.load(write_layer(tmp_path, text))
    with pytest.raises(SemanticError, match="dataset_version"):
        sem.dataset_version


def test_missing_date_field_raises(tmp_path):
    """date_field 键缺失属结构错误,load 即失败(空值走 validate,见参数化用例)。"""
    text = BASE_YAML.replace("date_field: dt\n", "")
    with pytest.raises(SemanticError, match="date_field"):
        Semantic.load(write_layer(tmp_path, text))


def test_dependency_cycle_raises(tmp_path):
    cycle = """  loop_a:
    label: 环 A
    expression: loop_b
    type: derived
    depends_on: [loop_b]
  loop_b:
    label: 环 B
    expression: loop_a
    type: derived
    depends_on: [loop_a]
"""
    text = BASE_YAML.replace("dimensions:", cycle + "dimensions:")
    with pytest.raises(SemanticError, match="成环"):
        Semantic.load(write_layer(tmp_path, text))


def test_unsupported_schema_version_raises(tmp_path):
    text = BASE_YAML.replace('schema_version: "2.0"', 'schema_version: "9.0"')
    with pytest.raises(SemanticError, match="schema_version"):
        Semantic.load(write_layer(tmp_path, text))


def test_non_mapping_root_raises(tmp_path):
    with pytest.raises(SemanticError):
        Semantic.load(write_layer(tmp_path, "- revenue\n- orders_cnt\n"))
