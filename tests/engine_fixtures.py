"""引擎集成测试的共享夹具:真实数据集上的 AttributionEngine + 临时语义层。

被 test_engine_decompose.py / test_engine_anomaly.py / test_sql_source.py 共用;
不是测试文件(pytest 只收集 test_*.py),不产生用例。

两条构造路径:
    real_engine()                     —— datasets/ecommerce-demo 自带的那张「地图」
    engine_with_layer(tmp_path, ...)  —— 临时语义层 + **同一份真实 parquet**
                                          (测「换一张地图,行为跟着地图走」)
临时语义层一律写 tmp_path,绝不碰 datasets/ 下的真实数据文件。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from attribution.engine import AttributionEngine
from harness.datasets import resolve_dataset
from tests.semantic_fixtures import write_layer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = PROJECT_ROOT / "datasets"
DATASET_ID = "ecommerce-demo"

# 归因窗口:对比期是埋了断崖下跌的 2026-06(618 大促月),基期取它前一个月
BASE_WINDOW = ("2026-05-01", "2026-05-31")
CMP_WINDOW = ("2026-06-01", "2026-06-30")

# 列名 -> SQL 片段:临时地图复用真实 parquet,只换声明
_TEMP_HEAD = """schema_version: "2.0"
dataset: engine-tests-tmp
dataset_version: "1.0.0"
fact_table: orders
date_field: date_id

dimensions:
  store:
    label: 门店
    table: dim_store
    key: store_id
    name_column: store_name
    hierarchy: [region, city, store_id]
"""

# 临时地图 ①:补上真实地图没有的 additive / ratio 声明,让这两条分支也能在真实数据上跑。
#   gross = net + discount_amount(各分项之和恒等于总量,分解模块会校验恒等式)
#   unit_price = gross / orders_cnt(比率:分子在前、分母在后)
TEMP_LAYER_YAML = _TEMP_HEAD + """
metrics:
  gross:
    label: 毛额
    expression: SUM(amount)
    type: additive
    depends_on: [amount]
  net:
    label: 净额
    expression: SUM(amount - discount)
    type: additive
    depends_on: [amount, discount]
  discount_amount:
    label: 优惠额
    expression: IFNULL(SUM(discount), 0)
    type: additive
    depends_on: [discount]
  orders_cnt:
    label: 订单数
    expression: COUNT(DISTINCT order_id)
    type: additive
    depends_on: [order_id]
  unit_price:
    label: 客单价
    expression: gross / NULLIF(orders_cnt, 0)
    type: derived
    depends_on: [gross, orders_cnt]

decompositions:
  - target: gross
    kind: additive
    factors: [net, discount_amount]
  - target: unit_price
    kind: ratio
    factors: [gross, orders_cnt]
"""

# 临时地图 ③:把 structural 声明挂到「依赖只有一个」的指标上。结构分解的泛化规则要求
# target 是「分子 / 分母」形(依赖恰两个),这类声明不该被接受,应在调用时报 ValueError。
STRUCTURAL_ARITY_LAYER_YAML = TEMP_LAYER_YAML.replace(
    "  - target: gross\n    kind: additive\n    factors: [net, discount_amount]",
    "  - target: gross\n    kind: structural\n    entity_dimension: store\n"
    "    factors: [product_mix, price]")
assert STRUCTURAL_ARITY_LAYER_YAML != TEMP_LAYER_YAML   # 替换必须真的生效,否则用例会假绿

# 临时地图 ②:一个「聚合结果恒为 NULL」的指标。真实数据里 amount 全为正数(最小 9.18),
# 故这段 CASE 每天都没有命中行 -> 整组聚合为 NULL,用来验证「没有值不等于 0」的口径。
NULL_LAYER_YAML = _TEMP_HEAD + """
metrics:
  amount_sum:
    label: 金额
    expression: SUM(amount)
    type: additive
    depends_on: [amount]
  never_positive:
    label: 恒空值
    expression: SUM(CASE WHEN amount < 0 THEN amount END)
    type: additive
    depends_on: [amount]
"""


@lru_cache(maxsize=1)
def dataset_info():
    """真实数据集包(语义层路径 + 数据目录),缓存:resolve 要读盘。"""
    return resolve_dataset(DATASET_ID, datasets_dir=DATASETS_DIR)


@lru_cache(maxsize=1)
def real_engine() -> AttributionEngine:
    """真实数据集上的引擎(缓存:构造要扫 parquet 并编译全部指标表达式)。"""
    info = dataset_info()
    return AttributionEngine(str(info.data_dir), str(info.semantic_path))


def engine_with_layer(tmp_path, text: str = TEMP_LAYER_YAML,
                      name: str = "semantic.yaml") -> AttributionEngine:
    """临时语义层 + 真实数据目录:验证「换地图,行为跟着地图走」。"""
    info = dataset_info()
    return AttributionEngine(str(info.data_dir), write_layer(tmp_path, text, name=name))
