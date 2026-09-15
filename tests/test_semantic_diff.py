# -*- coding: utf-8 -*-
"""semantic_diff 的级别判定契约:哪些改变是增量,哪些让历史数值失效。

夹具与辅助函数见 tests/semantic_diff_fixtures.py(内联 YAML + tmp_path,不依赖
真实数据集);退出码与输入形态的契约见 tests/test_semantic_diff_cli.py。

结论一句话:**加对象 / 改展示 = 增量;改口径 / 删对象 / 改路径或聚合语义 = 破坏性**。
"""

import pytest

from attribution.semantic_diff import LEVEL_BREAKING, LEVEL_INCREMENTAL, diff_layers, has_breaking
from tests.semantic_diff_fixtures import base_layer, diff_paths, only, write_layer


# ----------------------------------------------------------------------
# 无变更
# ----------------------------------------------------------------------
def test_identical_layers_report_nothing(tmp_path):
    """完全相同的两版 -> 空列表(同一路径、不同路径都算)。"""
    old_path = write_layer(tmp_path, base_layer(), "old.yaml")
    new_path = write_layer(tmp_path, base_layer(), "new.yaml")
    assert diff_layers(old_path, new_path) == []
    assert diff_layers(old_path, old_path) == []
    assert not has_breaking([])


def test_both_sides_missing_dataset_version_is_not_a_change(tmp_path):
    """两侧都缺 dataset_version -> 不算变更(缺失由 §3.5 校验负责报,diff 不重复报)。"""
    without_version = base_layer()
    del without_version["dataset_version"]
    other = base_layer()
    del other["dataset_version"]
    assert diff_paths(tmp_path, without_version, other) == []


# ----------------------------------------------------------------------
# 指标:新增 / 删除 / 改口径 / 改展示名 / 改依赖
# ----------------------------------------------------------------------
def test_added_metric_is_incremental(tmp_path):
    """加指标 = 增量(§3.6②:老 case 不受影响)。"""
    new_layer = base_layer()
    new_layer["metrics"]["ticket"] = {
        "label": "工单数", "expression": "COUNT(ticket_id)", "depends_on": ["ticket_id"]}
    changes = diff_paths(tmp_path, base_layer(), new_layer)
    assert len(changes) == 1
    assert (changes[0].kind, changes[0].level, changes[0].path) == (
        "added", LEVEL_INCREMENTAL, "metrics.ticket")
    assert not has_breaking(changes)


def test_removed_metric_is_breaking(tmp_path):
    """删指标 = 破坏性(引用它的 case 直接失效)。"""
    new_layer = base_layer()
    del new_layer["metrics"]["basket"]
    change = only(diff_paths(tmp_path, base_layer(), new_layer), "metrics.basket")
    assert (change.kind, change.level) == ("removed", LEVEL_BREAKING)
    assert "失效" in change.detail


def test_expression_change_is_breaking(tmp_path):
    """改 expression(口径)= 破坏性:历史数值不可比,贡献区间失效。"""
    new_layer = base_layer()
    new_layer["metrics"]["revenue"]["expression"] = "SUM(net_amount)"
    new_layer["metrics"]["revenue"]["depends_on"] = ["net_amount"]   # 引用随口径一起改
    changes = diff_paths(tmp_path, base_layer(), new_layer)
    change = only(changes, "metrics.revenue.expression")
    assert (change.kind, change.level) == ("modified", LEVEL_BREAKING)
    assert "不可比" in change.detail and "失效" in change.detail
    assert only(changes, "metrics.revenue.depends_on").level == LEVEL_BREAKING


def test_label_change_is_incremental(tmp_path):
    """只改 label = 增量(仅展示变化)。"""
    new_layer = base_layer()
    new_layer["metrics"]["basket"]["label"] = "件单价"
    change = only(diff_paths(tmp_path, base_layer(), new_layer), "metrics.basket.label")
    assert (change.kind, change.level) == ("modified", LEVEL_INCREMENTAL)
    assert "展示名" in change.detail


def test_depends_on_reorder_is_not_a_change(tmp_path):
    """依赖是无序集合(编译与校验都按集合处理)-> 纯换序不产生变更条目。"""
    new_layer = base_layer()
    new_layer["metrics"]["basket"]["depends_on"] = ["orders_cnt", "revenue"]
    assert diff_paths(tmp_path, base_layer(), new_layer) == []


def test_depends_on_membership_change_is_breaking(tmp_path):
    """依赖集合的成员变了 = 口径变了 -> 破坏性。"""
    new_layer = base_layer()
    new_layer["metrics"]["basket"]["depends_on"] = ["revenue", "orders_cnt", "discount_amt"]
    change = only(diff_paths(tmp_path, base_layer(), new_layer), "metrics.basket.depends_on")
    assert change.level == LEVEL_BREAKING
    assert "依赖集合" in change.detail


@pytest.mark.parametrize("field, level", [
    ("type", LEVEL_BREAKING),
    ("source", LEVEL_BREAKING),
    ("unit", LEVEL_INCREMENTAL),
])
def test_metric_field_levels(tmp_path, field, level):
    """指标其余字段:换类型 / 换事实表来源改的是算法,换单位只影响展示。"""
    new_layer = base_layer()
    new_layer["metrics"]["revenue"][field] = "changed_value"
    change = only(diff_paths(tmp_path, base_layer(), new_layer), f"metrics.revenue.{field}")
    assert change.level == level


# ----------------------------------------------------------------------
# 时间聚合:sum -> last 是最严重的一类(§1.3 静默错误)
# ----------------------------------------------------------------------
def test_time_aggregation_change_is_breaking_and_names_silent_error(tmp_path):
    """改 time_aggregation = 破坏性;sum -> last 的 detail 必须点名 §1.3 静默错误。"""
    old_layer = base_layer()
    old_layer["metrics"]["balance"]["time_aggregation"] = "sum"   # 旧地图:对快照指标求和
    new_layer = base_layer()                                      # 新地图:改为取期末值
    change = only(diff_paths(tmp_path, old_layer, new_layer), "metrics.balance.time_aggregation")
    assert (change.kind, change.level) == ("modified", LEVEL_BREAKING)
    assert "静默错误" in change.detail and "§1.3" in change.detail
    assert "sum" in change.detail and "last" in change.detail


# ----------------------------------------------------------------------
# 下钻层级:末尾追加 = 增量;顺序 / 插入 / 删除 = 破坏性
# ----------------------------------------------------------------------
def test_hierarchy_appended_level_is_incremental(tmp_path):
    """末尾追加一层 = 增量:既有层级的顺序与序号不变,老 case 的下钻路径仍成立。"""
    new_layer = base_layer()
    new_layer["dimensions"]["area"]["hierarchy"] = ["country", "area_id", "city"]
    change = only(diff_paths(tmp_path, base_layer(), new_layer), "dimensions.area.hierarchy")
    assert (change.kind, change.level) == ("added", LEVEL_INCREMENTAL)
    assert "追加" in change.detail


def test_hierarchy_order_change_is_breaking(tmp_path):
    """改 hierarchy 顺序 = 破坏性:下钻路径变了,required_depth 要复查。"""
    new_layer = base_layer()
    new_layer["dimensions"]["area"]["hierarchy"] = ["area_id", "country"]
    change = only(diff_paths(tmp_path, base_layer(), new_layer), "dimensions.area.hierarchy")
    assert (change.kind, change.level) == ("modified", LEVEL_BREAKING)
    assert "required_depth" in change.detail


def test_hierarchy_inserted_level_is_breaking(tmp_path):
    """中间插一层 = 破坏性:既有层级的序号平移了(不是「加层级」那种增量)。"""
    new_layer = base_layer()
    new_layer["dimensions"]["area"]["hierarchy"] = ["country", "province", "area_id"]
    change = only(diff_paths(tmp_path, base_layer(), new_layer), "dimensions.area.hierarchy")
    assert (change.kind, change.level) == ("modified", LEVEL_BREAKING)


def test_hierarchy_removed_level_is_breaking(tmp_path):
    """删除层级 = 破坏性:沿该层的切片与下钻直接失效。"""
    new_layer = base_layer()
    new_layer["dimensions"]["area"]["hierarchy"] = ["country"]
    change = only(diff_paths(tmp_path, base_layer(), new_layer), "dimensions.area.hierarchy")
    assert (change.kind, change.level) == ("removed", LEVEL_BREAKING)
    assert "失效" in change.detail


# ----------------------------------------------------------------------
# 维度与头部字段的级别表
# ----------------------------------------------------------------------
@pytest.mark.parametrize("field, value, level", [
    ("key", "other_id", LEVEL_BREAKING),
    ("table", "dim_other", LEVEL_BREAKING),
    ("type", "derived", LEVEL_BREAKING),
    ("drill_priority", 7, LEVEL_INCREMENTAL),
    ("name_column", "other_name", LEVEL_INCREMENTAL),
    ("label", "地区", LEVEL_INCREMENTAL),
])
def test_dimension_field_levels(tmp_path, field, value, level):
    """维度字段:改 key / table / 类型换的是切片身份,改优先级与展示列只影响取用。

    path 用 YAML 里的键名,故维度类型写作 type(解析后的字段名是 kind)。
    """
    new_layer = base_layer()
    new_layer["dimensions"]["area"][field] = value
    change = only(diff_paths(tmp_path, base_layer(), new_layer), f"dimensions.area.{field}")
    assert change.level == level


@pytest.mark.parametrize("field, level", [
    ("dataset", LEVEL_BREAKING),
    ("fact_table", LEVEL_BREAKING),
    ("date_field", LEVEL_BREAKING),
    ("schema_version", LEVEL_BREAKING),
    ("dataset_version", LEVEL_INCREMENTAL),
])
def test_header_field_levels(tmp_path, field, level):
    """头部字段:换地图 / 换事实表 / 换时间轴 / 换结构规范 = 破坏性;版本号本身 = 增量。"""
    new_layer = base_layer()
    new_layer[field] = "2.9" if field.endswith("version") else "changed_value"
    change = only(diff_paths(tmp_path, base_layer(), new_layer), field)
    assert change.level == level


def test_added_dimension_is_incremental(tmp_path):
    """加维度 = 增量;删维度 = 破坏性(引用它的 case 直接失效)。"""
    added = base_layer()
    added["dimensions"]["tenant"] = {"label": "租户", "table": "dim_tenant",
                                     "key": "tenant_id", "hierarchy": ["tenant_id"]}
    change = only(diff_paths(tmp_path, base_layer(), added), "dimensions.tenant")
    assert (change.kind, change.level) == ("added", LEVEL_INCREMENTAL)

    removed = base_layer()
    del removed["dimensions"]["area"]
    change = only(diff_paths(tmp_path, base_layer(), removed), "dimensions.area")
    assert (change.kind, change.level) == ("removed", LEVEL_BREAKING)


# ----------------------------------------------------------------------
# 三段知识块:分解声明 / 促销日历 / 口径陷阱
# ----------------------------------------------------------------------
def test_added_caveats_is_incremental(tmp_path):
    """加 caveats = 增量(§3.6②);删 caveats 也只影响知识,同样是增量。"""
    added = base_layer()
    added["caveats"] = [*added["caveats"], "退货口径自 6 月起调整"]
    change = only(diff_paths(tmp_path, base_layer(), added), "caveats")
    assert (change.kind, change.level) == ("modified", LEVEL_INCREMENTAL)
    assert "仅知识展示" in change.detail

    removed = base_layer()
    removed["caveats"] = []
    change = only(diff_paths(tmp_path, base_layer(), removed), "caveats")
    assert change.level == LEVEL_INCREMENTAL


def test_added_decomposition_is_incremental_but_modified_is_breaking(tmp_path):
    """分解声明:新增 target = 增量;改已有 target 的因子 = 破坏性。"""
    added = base_layer()
    added["decompositions"] = [*added["decompositions"],
                               {"target": "orders_cnt", "kind": "additive",
                                "factors": ["web_orders", "app_orders"]}]
    change = only(diff_paths(tmp_path, base_layer(), added), "decompositions.orders_cnt")
    assert (change.kind, change.level) == ("added", LEVEL_INCREMENTAL)

    modified = base_layer()
    modified["decompositions"][0]["factors"] = ["basket"]
    change = only(diff_paths(tmp_path, base_layer(), modified), "decompositions.revenue")
    assert change.level == LEVEL_BREAKING
    assert "期望结构失效" in change.detail


def test_calendar_change_is_incremental(tmp_path):
    """促销日历是知识块:增删改都不改变任何数值 -> 增量。"""
    new_layer = base_layer()
    new_layer["time"] = {"calendar": {"promos": [
        {"name": "开年大促", "range": ["2026-01-01", "2026-01-03"], "note": "预期脉冲"}]}}
    change = only(diff_paths(tmp_path, base_layer(), new_layer), "time.calendar.promos")
    assert change.level == LEVEL_INCREMENTAL
    assert "仅知识展示" in change.detail


# ----------------------------------------------------------------------
# 输出排序:破坏性在前,同级内按 path(可比对才看得出改了什么)
# ----------------------------------------------------------------------
def test_changes_are_sorted_breaking_first_then_by_path(tmp_path):
    """输出顺序:破坏性在前,同级内按 path;重复对比结果完全一致。"""
    new_layer = base_layer()
    new_layer["dataset_version"] = "1.1.0"                              # 增量:元信息
    new_layer["dimensions"]["area"]["hierarchy"] = ["country", "area_id", "city"]  # 增量:追加
    new_layer["metrics"]["ticket"] = {"label": "工单数", "expression": "COUNT(ticket_id)",
                                      "depends_on": ["ticket_id"]}      # 增量:新增指标
    new_layer["metrics"]["revenue"]["expression"] = "SUM(net_amount)"   # 破坏性:改口径
    new_layer["metrics"]["revenue"]["depends_on"] = ["net_amount"]
    del new_layer["metrics"]["basket"]                                  # 破坏性:删指标

    changes = diff_paths(tmp_path, base_layer(), new_layer)
    assert [change.path for change in changes] == [
        "metrics.basket", "metrics.revenue.depends_on", "metrics.revenue.expression",
        "dataset_version", "dimensions.area.hierarchy", "metrics.ticket",
    ]
    assert [change.level for change in changes] == [
        LEVEL_BREAKING, LEVEL_BREAKING, LEVEL_BREAKING,
        LEVEL_INCREMENTAL, LEVEL_INCREMENTAL, LEVEL_INCREMENTAL,
    ]
    assert changes == diff_paths(tmp_path, base_layer(), new_layer)   # 稳定可比对
