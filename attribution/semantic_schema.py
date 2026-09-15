"""语义层这张地图的**定义**与**只看地图就能判定**的规则。

契约:docs/REUSE_DESIGN.md §3.2(schema v2) / §3.5(自洽性校验) / §3.6④(可达性)

三块内容:词汇表(指标类型 / 时间聚合 / 维度类型);定义实体(Metric / Dimension)与
YAML 原始节点到实体的 fail-fast 构造;校验规则(声明自洽、维度自洽、依赖无环)——一律
**纯函数**:入参只有实体与文档原件,出参是问题描述列表(一次报全、不抛异常)。
需要「地图之外的事实」的规则(列是否存在、表达式能否编译、可达性)留在
attribution.semantic —— 那里才持有列信息与编译缓存。

边界:本模块只读 YAML(load_yaml 是唯一的 IO),不编译 SQL、不碰 DuckDB、
不持有编译缓存、不认识 harness/ 与引擎;依赖单向:`semantic` → `semantic_schema`,
不反向;跨模块只用本模块 __all__ 里的名字。

公共名字由 attribution.semantic 统一再导出,外部一律
`from attribution.semantic import Metric, Dimension, SemanticError`。
"""

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

import yaml

# 分解声明与日历节点的定义、解析与纯规则:独立成模块(行数约束),这里再导出
from attribution.semantic_decompose import (  # noqa: F401
    Decomposition, find_decomposition_problems, parse_calendar, parse_decompositions,
)

__all__ = [
    "Columns", "Decomposition", "Dimension", "Metric", "SchemaDocument", "SemanticError",
    "find_declaration_problems", "find_decomposition_problems",
    "find_dependency_cycle", "find_dimension_problems", "load_yaml", "parse_layer",
]

# 真实表结构:表名 -> 列名集合(调用方从 DuckDB DESCRIBE 或 parquet 扫描得到)
Columns = Mapping[str, Collection[str]]

# 指标类型
TYPE_ADDITIVE = "additive"            # 可直接跨任意维度聚合
TYPE_SEMI_ADDITIVE = "semi_additive"  # 跨实体可加,跨时间不可加(如 MRR / DAU / 余额)
TYPE_DERIVED = "derived"              # 同粒度派生(如 客单价 = gmv / 订单数)
TYPE_RATIO = "ratio"                  # 跨粒度比率(分子分母人群/粒度不同),走两阶段计算

# 时间聚合语义。semi_additive 必须显式声明且不得为 sum
AGG_SUM = "sum"
AGG_LAST = "last"
AGG_AVG = "avg"
AGG_MAX = "max"

# 维度类型
DIM_TABLE = "table"      # 来自维度表
DIM_DERIVED = "derived"  # 从行为派生(如同期群)

# 事实表别名约定:编译表达式与默认编译缓存都以它为默认别名(与引擎侧的约定一致)
DEFAULT_ALIAS = "o"

# ---- 校验用枚举(避免散落字面量)----
_KNOWN_TYPES = frozenset({TYPE_ADDITIVE, TYPE_SEMI_ADDITIVE, TYPE_DERIVED, TYPE_RATIO})
_KNOWN_AGGS = frozenset({AGG_SUM, AGG_LAST, AGG_AVG, AGG_MAX})
_KNOWN_DIM_KINDS = frozenset({DIM_TABLE, DIM_DERIVED})
_TWO_STAGE = "two_stage"          # ratio 指标的两阶段计算声明
_DEFAULT_SCHEMA_VERSION = "1.0"   # 未声明 schema_version 的老语义层的兜底
_SUPPORTED_MAJORS = frozenset({"1", "2"})


class SemanticError(ValueError):
    """语义层加载或校验失败。

    继承 ValueError 是**兼容性要求**:改造前 engine 对「未知指标 / 未知维度」抛 ValueError,
    按 ValueError 捕获的调用方不能因重构而漏接;语义层错误本质都是「传进来的值不合法」。
    """


@dataclass(frozen=True)
class Metric:
    """一个指标的定义(语义层 metrics 里的一条)。"""

    name: str
    label: str
    expression: str
    type: str = TYPE_ADDITIVE
    time_aggregation: str = AGG_SUM
    depends_on: tuple[str, ...] = ()
    unit: str | None = None
    source: str | None = None       # 取自哪张事实表;None 表示默认事实表


@dataclass(frozen=True)
class Dimension:
    """一个维度的定义(语义层 dimensions 里的一条)。"""

    name: str
    label: str
    key: str
    table: str
    hierarchy: tuple[str, ...]      # 下钻层级,从粗到细
    name_column: str | None = None  # key 层的显示名列;None 表示用 key 值本身
    kind: str = DIM_TABLE
    drill_priority: int | None = None


# ----------------------------------------------------------------------
# 模块级私有工具:YAML 取值与对象构造(结构错误一律 fail-fast)
# ----------------------------------------------------------------------
def _opt_str(value: object) -> str | None:
    """去空白字符串;None / 空串 -> None。"""
    text = None if value is None else str(value).strip()
    return text or None


def _required_str(raw: Mapping, key: str) -> str:
    text = _opt_str(raw.get(key))
    if text is None:
        raise SemanticError(f"语义层缺少必需的 {key}")
    return text


def _required_map(raw: Mapping, key: str) -> Mapping:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise SemanticError(f"语义层缺少 {key}(必须是映射)")
    return value


def _str_list(entry: Mapping, key: str) -> tuple[str, ...]:
    """取字符串列表字段(缺省 -> 空元组;写成标量属结构错误)。"""
    value = entry.get(key) or ()
    if not isinstance(value, (list, tuple)):
        raise SemanticError(f"{key} 必须是列表,得到 {value!r}")
    return tuple(str(item) for item in value)


def _build_metric(name: str, entry: object) -> Metric:
    """由 YAML 条目构造 Metric;缺 expression 属结构错误。"""
    if not isinstance(entry, Mapping):
        raise SemanticError(f"指标 {name} 的定义必须是映射")
    expression = _opt_str(entry.get("expression"))
    if expression is None:
        raise SemanticError(f"指标 {name} 缺少 expression")
    return Metric(
        name=name, label=_opt_str(entry.get("label")) or name, expression=expression,
        type=_opt_str(entry.get("type")) or TYPE_ADDITIVE,
        time_aggregation=_opt_str(entry.get("time_aggregation")) or AGG_SUM,
        depends_on=_str_list(entry, "depends_on"),
        unit=_opt_str(entry.get("unit")), source=_opt_str(entry.get("source")),
    )


def _build_dimension(name: str, entry: object) -> Dimension:
    """由 YAML 条目构造 Dimension;缺失字段留空,交由校验报问题。"""
    if not isinstance(entry, Mapping):
        raise SemanticError(f"维度 {name} 的定义必须是映射")
    priority = entry.get("drill_priority")
    if priority is not None and not isinstance(priority, int):
        raise SemanticError(f"维度 {name} 的 drill_priority 必须是整数")
    return Dimension(
        name=name, label=_opt_str(entry.get("label")) or name,
        key=_opt_str(entry.get("key")) or "", table=_opt_str(entry.get("table")) or "",
        hierarchy=_str_list(entry, "hierarchy"), name_column=_opt_str(entry.get("name_column")),
        kind=_opt_str(entry.get("type")) or DIM_TABLE, drill_priority=priority,
    )


def load_yaml(path: str) -> object:
    """读取并解析 YAML;IO / 语法错误统一转成 SemanticError。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except OSError as err:
        raise SemanticError(f"语义层文件无法读取: {path} ({err})") from err
    except yaml.YAMLError as err:
        raise SemanticError(f"语义层 YAML 解析失败: {path} ({err})") from err


# ----------------------------------------------------------------------
# YAML 根节点 -> 定义实体(公共入口:parse_layer)
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class SchemaDocument:
    """语义层 YAML 的解析结果:头部(版本 / 数据集 / 事实表)+ 定义 + 原始条目。

    头部字段一律是归一化后的字符串(缺省给空串或兜底版本)——「值为空」不是结构错误,
    交给 §3.5 校验判定,解析层不重复报。
    """

    schema_version: str
    dataset: str
    dataset_version: str
    fact_table: str
    date_field: str
    metrics: Mapping[str, Metric]
    dimensions: Mapping[str, Dimension]
    raw_metrics: Mapping[str, object]        # 指标原始条目:判「是否显式声明」这类文档级规则用
    decompositions: tuple[Decomposition, ...]  # 分解声明(§4.2);未声明为空元组
    time_calendar: Mapping[str, object]        # time.calendar 节点原样保留(自由形状,§4.4)


def parse_layer(raw: Mapping) -> SchemaDocument:
    """把语义层 YAML 根节点解析成 SchemaDocument;结构错误一律 fail-fast。

    失败顺序固定:schema_version 受支持 -> fact_table / date_field 存在 -> metrics /
    dimensions 段是映射 -> 逐条构造指标与维度 -> decompositions 逐条构造 ->
    time.calendar 原样保留(自由形状,由消费方校验)。"""
    schema_version = _opt_str(raw.get("schema_version")) or _DEFAULT_SCHEMA_VERSION
    if schema_version.split(".")[0] not in _SUPPORTED_MAJORS:
        raise SemanticError(f"不支持的语义层 schema_version: {schema_version}")
    dataset = _opt_str(raw.get("dataset")) or ""
    dataset_version = _opt_str(raw.get("dataset_version")) or ""
    fact_table = _required_str(raw, "fact_table")
    if "date_field" not in raw:   # 键缺失属结构错误;取值是否为空由 validate 报(§3.5)
        raise SemanticError("语义层缺少必需的 date_field")
    date_field = _opt_str(raw.get("date_field")) or ""
    raw_metrics = _required_map(raw, "metrics")
    raw_dims = _required_map(raw, "dimensions")
    return SchemaDocument(
        schema_version=schema_version, dataset=dataset, dataset_version=dataset_version,
        fact_table=fact_table, date_field=date_field, raw_metrics=raw_metrics,
        metrics={n: _build_metric(n, e) for n, e in raw_metrics.items()},
        dimensions={n: _build_dimension(n, e) for n, e in raw_dims.items()},
        decompositions=parse_decompositions(raw),
        time_calendar=parse_calendar(raw),
    )


# ----------------------------------------------------------------------
# 校验规则(只看地图就能判定):实体 / 文档原件 -> 问题描述列表
# ----------------------------------------------------------------------
def find_declaration_problems(metric: Metric, declared: object) -> list[str]:
    """单个指标的**声明**问题:类型与时间聚合取值合法 + 两种特殊类型的显式声明要求。

    declared 是该指标在 YAML 里的原始条目——「必须显式声明」这类规则只能看文档原文,
    看构造出来的 Metric 无法区分「写没写」与「写了默认值」。"""
    entry = declared if isinstance(declared, Mapping) else {}
    checks = (
        (metric.type not in _KNOWN_TYPES,
         f"指标 {metric.name} 的类型 '{metric.type}' 未知"),
        (metric.time_aggregation not in _KNOWN_AGGS,
         f"指标 {metric.name} 的 time_aggregation '{metric.time_aggregation}' 未知"),
        (metric.type == TYPE_SEMI_ADDITIVE and entry.get("time_aggregation") is None,
         f"指标 {metric.name} 是 semi_additive,必须显式声明 time_aggregation"),
        (metric.type == TYPE_SEMI_ADDITIVE and metric.time_aggregation == AGG_SUM,
         f"指标 {metric.name} 是 semi_additive,time_aggregation 不得为 sum"),
        (metric.type == TYPE_RATIO and _opt_str(entry.get("computation")) != _TWO_STAGE,
         f"指标 {metric.name} 是 ratio 类型,必须声明 computation: two_stage"),
    )
    return [msg for hit, msg in checks if hit]


def find_dimension_problems(dimensions: Mapping[str, Dimension]) -> list[str]:
    """全部维度的自洽性问题(hierarchy / key / drill_priority),按定义顺序报全。"""
    problems: list[str] = []
    owners: dict[int, str] = {}
    for dim in dimensions.values():
        if dim.kind not in _KNOWN_DIM_KINDS:
            problems.append(f"维度 {dim.name} 的类型 '{dim.kind}' 未知")
        if not dim.hierarchy:
            problems.append(f"维度 {dim.name} 的 hierarchy 为空")
        else:
            dupes = [f for f, n in Counter(dim.hierarchy).items() if n > 1]
            if dupes:
                problems.append(f"维度 {dim.name} 的 hierarchy 存在重复字段: {', '.join(dupes)}")
        if dim.kind == DIM_TABLE:
            if not dim.key or not dim.table:
                problems.append(f"维度 {dim.name} 缺少 key 或 table")
            elif dim.key not in dim.hierarchy:
                problems.append(f"维度 {dim.name} 的 key '{dim.key}' 不在其 hierarchy 中")
        if dim.drill_priority is not None:
            owner = owners.setdefault(dim.drill_priority, dim.name)
            if owner != dim.name:
                problems.append(f"维度 {dim.name} 与 {owner} 的 "
                                f"drill_priority 重复({dim.drill_priority})")
    return problems


def find_dependency_cycle(metrics: Mapping[str, Metric]) -> list[str]:
    """返回一条依赖环的指标路径;无环返回空列表。"""
    state: dict[str, int] = {}   # 1 = 该指标及其下游已确认无环
    path: list[str] = []

    def visit(node: str) -> list[str]:
        if state.get(node) == 1:
            return []
        if node in path:   # 回到当前 DFS 路径上 -> 成环
            return path[path.index(node):] + [node]
        path.append(node)
        for dep in metrics[node].depends_on:
            if dep in metrics:
                found = visit(dep)
                if found:
                    return found
        path.pop()
        state[node] = 1
        return []

    for name in metrics:
        found = visit(name)
        if found:
            return found
    return []
