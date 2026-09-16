"""导入的语义层**草稿**生成:列统计 -> 语义层 dict(schema v2 的合法地图)。

从 harness/importer.py 迁出(那个文件到行数上限了)。分工不变:importer 负责
「文件 -> parquet + 统计」,这里负责「统计 -> 草稿语义层」,两者都是纯逻辑。

草稿 = 自动推断 + 三处身份改写 + 单表补丁:
    1. 身份三处必须写死:dataset 等于目录名、dataset_version 不能空也不能是 draft、
       fact_table 必须等于 parquet 的 stem(引擎按 stem 认表)
    2. 单表场景 dimensions 必为空(suggest_semantic_draft 只认「非事实表且有主键」的
       维度表),所以把低基数文本列提升为**派生维度**(层级字段住在事实表上,不 join)
    3. 指标 key 不得与事实表列名重合,否则引擎 load 时会把「指标依赖自身」判成依赖环

**草稿是给人改的**:它落进 datasets/.pending/,由 harness/import_confirm.py 在人工
确认(或显式跳过)之后才成为正式地图。所以这里保留 _todo 之外的痕迹也没关系 ——
反正它不会直接进 datasets/<id>/。
"""

from __future__ import annotations

from attribution.profile import (
    _ID_TOKEN,
    _LOW_CARD,
    _PK_AT_LEAST,
    _STRONG_DATE_TOKENS,
    _kind,
    _parts,
    suggest_semantic_draft,
)

__all__ = ["DATASET_VERSION", "FACT_TABLE", "PARQUET_NAME", "build_semantic"]

# 数据集包身份:事实表固定写 parquet 的 stem;版本号必须是可评估值,不能是 "draft"
FACT_TABLE = "fact"
PARQUET_NAME = "fact.parquet"
DATASET_VERSION = "import"


def build_semantic(stats: dict, dataset_id: str, date_field: str) -> dict:
    """列统计 -> 可装载的草稿语义层:改身份 + 补派生维度 + 指标 key 去重 + 删 _todo。"""
    draft = suggest_semantic_draft(stats)
    draft["dataset"] = dataset_id
    draft["dataset_version"] = DATASET_VERSION    # 坑 1:不能空、不能 draft
    draft["fact_table"] = FACT_TABLE              # 坑 4:必须等于 parquet stem
    draft["date_field"] = date_field              # 用最终确定的时间字段(含 override)
    add_derived_dimensions(draft, stats, date_field)
    unique_metric_names(draft, stats)             # 坑 5:指标 key 必须与列名错开
    draft.pop("_todo", None)                      # 这是给人确认的草稿,不留待办标记
    return draft


def unique_metric_names(draft: dict, stats: dict) -> None:
    """指标 key 不得与事实表列名重合:草稿指标 key == 数值列名,depends_on 又指向同一列,
    引擎 load 时的 find_dependency_cycle 会把这当成「指标依赖自身」抛成环(演示数据集的
    指标 key 与列名刻意错开所以从不触发;这批自动地图必须自己归一)。撞名的 key 追加
    _sum 后缀——label / expression / depends_on 不动,对外展示仍是原列名。
    """
    fact_cols = set((stats.get(draft["fact_table"]) or {}).keys())
    metrics = draft["metrics"]
    for name in list(metrics):
        if name not in fact_cols:
            continue
        new = f"{name}_sum"
        while new in metrics:
            new = f"{new}_sum"
        metrics[new] = metrics.pop(name)


def add_derived_dimensions(draft: dict, stats: dict, date_field: str) -> None:
    """低基数文本列 -> derived 维度(层级字段就在事实表上,引擎不 join 也能下钻)。

    过滤口径与 profile 的层级推断一致:只收基数 2~50 的文本列,剔除时间轴、代理键
    (唯一列 / ID 词元)与「像日期」的列。人工确认时用户可以在向导里改这份名单 ——
    这条自动规则剔掉的高基数列(如门店号),往往正是业务上最该下钻的那一层。
    """
    fact_cols = stats.get(draft["fact_table"], {})
    dimensions = draft.setdefault("dimensions", {})
    for column, stat in fact_cols.items():
        if column == date_field:
            continue
        if _kind(stat) != "OTHER":
            continue
        if not (2 <= stat["cardinality"] <= _LOW_CARD):
            continue
        if stat["uniqueness"] >= _PK_AT_LEAST:
            continue
        if _ID_TOKEN in _parts(column):
            continue
        if _parts(column) & _STRONG_DATE_TOKENS:
            continue
        dimensions[column] = {"label": column, "type": "derived",
                              "hierarchy": [column]}