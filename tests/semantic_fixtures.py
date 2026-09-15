"""语义层契约测试的共享夹具:一份合法的内联语义层 + 常用辅助函数。

被 test_semantic_schema_load.py 与 test_semantic_schema_validate.py 共用,
免得同一份 YAML 常量在两边各写一遍后互相漂移。本模块不是测试文件
(pytest 只收集 test_*.py),不会产生用例。

夹具全部是内联 YAML + tmp_path,不依赖任何真实数据集文件。
词汇刻意与 ecommerce-demo 不同,以证明 attribution.semantic 不认识任何具体数据集。
"""

from __future__ import annotations

import pytest

from attribution.expression import referenced_identifiers
from attribution.semantic import Semantic


def needs_compiler(func):
    """编译器未落地时把用例挂起(xfail);已落地则正常执行,不做屏蔽。"""
    try:
        referenced_identifiers("")
    except NotImplementedError:
        return pytest.mark.xfail(reason="等待 expression 编译器落地", strict=False)(func)
    except Exception:
        return func
    return func


# ----------------------------------------------------------------------
# 夹具:一份合法的语义层
# ----------------------------------------------------------------------
MRR_BLOCK = """  mrr:
    label: MRR
    expression: SUM(mrr_amount)
    type: semi_additive
    time_aggregation: last
    depends_on: [mrr_amount]
"""

BASE_YAML = """schema_version: "2.0"
dataset: unit-test-shop
dataset_version: "1.2.3"
fact_table: fact_sales
date_field: dt

metrics:
  revenue:
    label: 营收
    expression: SUM(amount)
    type: additive
    depends_on: [amount]
  orders_cnt:
    label: 订单数
    expression: COUNT(DISTINCT order_id)
    type: additive
    depends_on: [order_id]
  aov:
    label: 客单价
    expression: revenue / NULLIF(orders_cnt, 0)
    type: derived
    depends_on: [revenue, orders_cnt]
""" + MRR_BLOCK + """
dimensions:
  region:
    label: 区域
    table: dim_region
    key: region_id
    hierarchy: [country, region_id]
  day:
    label: 日期
    table: dim_day
    key: dt
    hierarchy: [year, dt]
"""

# 表名 -> 列名集合(仅供测试;真实调用方由 DuckDB DESCRIBE 得到)
BASE_COLUMNS: dict[str, set[str]] = {
    "fact_sales": {"order_id", "dt", "amount", "mrr_amount", "region_id"},
    "dim_region": {"region_id", "region_name", "country"},
    "dim_day": {"dt", "year", "month"},
}


def write_layer(tmp_path, text: str, name: str = "semantic.yaml") -> str:
    """把内联 YAML 写到临时目录,返回路径。"""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def load_problems(tmp_path, text: str) -> list[str]:
    """加载(不传 columns)后取 validate 的问题列表。"""
    return Semantic.load(write_layer(tmp_path, text)).validate()
