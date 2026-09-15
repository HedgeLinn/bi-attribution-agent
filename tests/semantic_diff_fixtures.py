# -*- coding: utf-8 -*-
"""semantic_diff 契约测试的共享夹具:一份内联语义层 + 常用辅助函数。

被 test_semantic_diff.py(级别判定)与 test_semantic_diff_cli.py(退出码与输入形态)
共用,免得同一份 YAML 常量在两边各写一遍后互相漂移。本模块不是测试文件
(pytest 只收集 test_*.py),不会产生用例。

夹具全部是**内联 YAML + tmp_path**,不依赖任何真实数据集文件(数据集会搬家、
也会增删);词汇刻意与真实数据集不同,以证明判定逻辑不认识任何具体数据集。

级别判定的口径与边界理由见 attribution/semantic_diff.py 的模块文档。
"""

import copy

import yaml

from attribution.semantic_diff import Change, diff_layers

# 一份合法的语义层(两版对比的基准)。字段齐全,便于逐项只改一个字段
BASE_YAML = """schema_version: "2.0"
dataset: diff-fixture
dataset_version: "1.0.0"
fact_table: fact_events
date_field: event_day

metrics:
  revenue:
    label: 营收
    expression: SUM(amount)
    type: additive
    time_aggregation: sum
    depends_on: [amount]
  orders_cnt:
    label: 订单数
    expression: COUNT(DISTINCT order_id)
    type: additive
    depends_on: [order_id]
  basket:
    label: 客单价
    expression: revenue / NULLIF(orders_cnt, 0)
    type: derived
    depends_on: [revenue, orders_cnt]
  balance:
    label: 余额
    expression: AVG(balance_amt)
    type: semi_additive
    time_aggregation: last
    depends_on: [balance_amt]

decompositions:
  - target: revenue
    kind: multiplicative
    factors: [basket, orders_cnt]

dimensions:
  area:
    label: 区域
    table: dim_area
    key: area_id
    name_column: area_name
    hierarchy: [country, area_id]
  event_day:
    label: 日期
    table: dim_calendar
    key: event_day
    hierarchy: [year, event_day]

caveats:
  - 促销期后自然回落属预期
"""


def base_layer() -> dict:
    """每次给一份互不影响的基准语义层。"""
    return copy.deepcopy(yaml.safe_load(BASE_YAML))


def write_layer(tmp_path, layer: dict, name: str) -> str:
    """把语义层写到临时目录,返回路径。"""
    path = tmp_path / name
    path.write_text(yaml.safe_dump(layer, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return str(path)


def diff_paths(tmp_path, old_layer: dict, new_layer: dict) -> list[Change]:
    """两版语义层 -> 变更列表(都经文件路径输入)。"""
    return diff_layers(write_layer(tmp_path, old_layer, "old.yaml"),
                       write_layer(tmp_path, new_layer, "new.yaml"))


def only(changes: list[Change], path: str) -> Change:
    """取指定 path 的唯一一条变更(顺带钉死「没有多报」)。"""
    matched = [change for change in changes if change.path == path]
    assert len(matched) == 1, f"期望恰好 1 条 path={path} 的变更,实得 {[c.path for c in changes]}"
    return matched[0]
