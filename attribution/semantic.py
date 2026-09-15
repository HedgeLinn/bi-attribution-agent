"""语义层服务:加载、访问、编译缓存,以及需要「地图之外事实」的校验。

契约:docs/REUSE_DESIGN.md §3.2(schema v2) / §3.5(自洽性校验) / §3.6④(可达性)

语义层是**下钻归因的地图**,本模块是它的唯一入口。约束:不生成 SQL(编译交给
attribution.expression)、不碰 DuckDB、不认识 harness/ 与引擎;校验一次报全
(validate 返回问题列表),不是抛第一个就停。

职责分工:地图的**定义、解析与「只看地图就能判定」的规则**住 attribution.semantic_schema
(纯函数);**需要地图之外事实的规则**留在本地——它们要用这里持有的列信息与编译缓存。
两个方向都只经由公共名字通信;公共入口仍是这里:`from attribution.semantic import ...`。
"""

from collections.abc import Collection, Mapping
from typing import Any

from attribution.expression import ExpressionError, compile_expression, referenced_identifiers
from attribution.semantic_decompose import match_decomposition
# 定义、词汇与纯规则住 semantic_schema,这里再导出(对外契约是从 semantic 取,见 __all__)
from attribution.semantic_schema import (  # noqa: F401
    AGG_AVG, AGG_LAST, AGG_MAX, AGG_SUM, Columns, DEFAULT_ALIAS, DIM_DERIVED, DIM_TABLE,
    TYPE_ADDITIVE, TYPE_DERIVED, TYPE_RATIO, TYPE_SEMI_ADDITIVE, Decomposition, Dimension,
    Metric, SemanticError, find_declaration_problems, find_decomposition_problems,
    find_dependency_cycle, find_dimension_problems, load_yaml, parse_layer,
)

__all__ = ["Decomposition", "Dimension", "Metric", "Semantic", "SemanticError"]


class Semantic:
    """语义层。通过 load() 构造,不要直接实例化。"""

    def __init__(self, raw: Mapping, columns: Columns | None = None) -> None:
        """解析 + 结构性 fail-fast;自洽性 / 可达性交给 validate / check_reachability。"""
        self._document = parse_layer(raw)
        self._field_to_dim: dict[str, str] = {}
        for dim in self._document.dimensions.values():
            for field in dim.hierarchy:
                self._field_to_dim.setdefault(field, dim.name)
        self._columns = ({str(t): frozenset(map(str, cols)) for t, cols in columns.items()}
                         if columns is not None else None)
        self._compiled: dict[str, dict[str, str]] = {}
        cycle = find_dependency_cycle(self._document.metrics)   # 引用成环:load 时就失败
        if cycle:
            raise SemanticError(f"指标依赖成环: {' -> '.join(cycle)}")
        if self._columns is not None:
            # 编译在 load 时算好并缓存;引用不存在的列 / 指标在这里失败
            self._compiled[DEFAULT_ALIAS] = self._compile_all(DEFAULT_ALIAS)

    @classmethod
    def load(cls, path: str, columns: Columns | None = None) -> "Semantic":
        """加载语义层并校验。

        path: 语义层 YAML 路径;columns: 表名 -> 列名集合(编译识别裸列名 + 可达性校验,
        传 None 跳过一切依赖列名的校验)。
        异常:SemanticError —— YAML 非法、schema_version 不支持、或结构校验不通过。
        """
        raw = load_yaml(path)
        if not isinstance(raw, Mapping):
            raise SemanticError(f"语义层根节点必须是映射: {path}")
        return cls(raw, columns)

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------
    @property
    def dataset(self) -> str:
        """数据集标识(如 'ecommerce-demo')。"""
        return self._document.dataset

    @property
    def schema_version(self) -> str:
        """语义层的**结构规范**版本(如 '2.0')。"""
        return self._document.schema_version

    @property
    def dataset_version(self) -> str:
        """这张地图的**实例**版本。评估结果必须绑定它(fail-fast 取不到就报错)。"""
        if not self._document.dataset_version:
            raise SemanticError("语义层缺少 dataset_version(评估结果必须绑定数据集版本)")
        return self._document.dataset_version

    @property
    def fact_table(self) -> str:
        return self._document.fact_table

    @property
    def date_field(self) -> str:
        return self._document.date_field

    # ------------------------------------------------------------------
    # 指标与维度
    # ------------------------------------------------------------------
    @property
    def metrics(self) -> Mapping[str, Metric]:
        return self._document.metrics

    @property
    def dimensions(self) -> Mapping[str, Dimension]:
        return self._document.dimensions

    def metric(self, name: str) -> Metric:
        """取一个指标;不存在则抛 SemanticError。"""
        if name not in self._document.metrics:
            raise SemanticError(f"语义层没有指标 {name}")
        return self._document.metrics[name]

    def dimension(self, name: str) -> Dimension:
        """取一个维度;不存在则抛 SemanticError。"""
        if name not in self._document.dimensions:
            raise SemanticError(f"语义层没有维度 {name}")
        return self._document.dimensions[name]

    def field_to_dimension(self, field: str) -> str | None:
        """维度层级字段 -> 所属维度名;不是任何维度的字段则返回 None。

        供引擎推导「这个字段需要 join 哪张维度表」。
        """
        return self._field_to_dim.get(field)

    def all_levels(self) -> dict[str, tuple[str, ...]]:
        """维度名 -> 该维度的全部层级字段。供渲染工具 schema 的 enum 用。"""
        return {name: dim.hierarchy for name, dim in self._document.dimensions.items()}

    # ------------------------------------------------------------------
    # 分解声明与日历(§4.2 / §4.4)
    # ------------------------------------------------------------------
    @property
    def decompositions(self) -> tuple[Decomposition, ...]:
        """全部分解声明;未声明为空元组。"""
        return self._document.decompositions

    @property
    def time_calendar(self) -> Mapping[str, Any] | None:
        """time.calendar 节点(自由形状,原样透传);未声明返回 None。"""
        return self._document.time_calendar or None

    def decomposition_for(self, target: str, factors: Collection[str]) -> Decomposition:
        """查 (target, factors) 的分解声明;未命中抛 SemanticError(纯匹配见 semantic_decompose)。"""
        return match_decomposition(self._document.decompositions, target, factors)

    # ------------------------------------------------------------------
    # 编译后的指标表达式
    # ------------------------------------------------------------------
    def agg_expr(self, metric: str, alias: str = DEFAULT_ALIAS) -> str:
        """返回该指标**编译后**的 DuckDB 聚合表达式。

        derived 指标按依赖顺序展开为其底层聚合(如 客单价 -> SUM(amount)/COUNT(...))。
        编译结果在 load 时算好并缓存;引用成环或引用不存在的指标在 load 时就会失败。

        异常:SemanticError —— metric 不存在,或其表达式无法编译。
        """
        if metric not in self._document.metrics:
            raise SemanticError(f"语义层没有指标 {metric}")
        if alias not in self._compiled:
            if self._columns is None:
                raise SemanticError(f"load 时未提供 columns,无法编译指标 {metric} 的表达式")
            self._compiled[alias] = self._compile_all(alias)   # 非默认别名:惰性编译并缓存
        return self._compiled[alias][metric]

    def render_overview(self) -> str:
        """渲染给模型看的语义层摘要(可用指标 + 维度下钻层级),取代 tools.py 里的硬编码文本。"""
        metrics = [f"  - {m.name}({m.label}): {m.expression} "
                   f"[{m.type}{' · 单位 ' + m.unit if m.unit else ''}]"
                   for m in self._document.metrics.values()]
        levels = [f"  - {d.name}({d.label}): {' -> '.join(d.hierarchy)}"
                  for d in self._document.dimensions.values()]
        return "\n".join(["可用指标:", *metrics,
                          "维度与下钻层级(从左到右由粗到细):", *levels])

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def validate(self) -> list[str]:
        """自洽性校验(§3.5)。返回**问题描述列表**,空列表表示通过;一次报全,不抛异常。

        覆盖:expression 可编译 / refs 与 depends_on 一致 / 依赖无环且指标存在 /
        semi_additive 必须显式声明 time_aggregation 且不得为 sum / ratio 必须 two_stage /
        date_field 非空 / hierarchy 非空且不含重复项 / key ∈ hierarchy / drill_priority 无冲突。

        报告顺序固定:fact_table -> date_field -> 指标(逐条)-> 维度 -> 依赖环。
        纯地图规则来自 semantic_schema,依赖列信息与编译结果的规则在本地。
        """
        problems: list[str] = []
        if not self._document.fact_table:
            problems.append("fact_table 为空")
        if not self._document.date_field:
            problems.append("date_field 为空")
        if not self._document.metrics:
            problems.append("metrics 为空:没有定义任何指标")
        if not self._document.dimensions:
            problems.append("dimensions 为空:没有定义任何维度")
        for name, metric in self._document.metrics.items():
            problems += find_declaration_problems(metric, self._document.raw_metrics.get(name))
            problems += self._reference_problems(metric)
        problems += find_dimension_problems(self._document.dimensions)
        problems += find_decomposition_problems(
            self._document.decompositions, self._document.metrics, self._document.dimensions)
        cycle = find_dependency_cycle(self._document.metrics)
        if cycle:
            problems.append(f"指标依赖成环: {' -> '.join(cycle)}")
        return problems

    def check_reachability(self) -> list[str]:
        """地图可达性校验(§3.6④)。返回问题列表,空列表表示通过;columns 为 None 时整体跳过。

        覆盖:每个维度的 key 在事实表列里存在(能 join)/ 每个 hierarchy 字段在对应维度表
        里存在 / 没有孤立维度(层级字段一个都不存在)/ 至少有一个维度可用于下钻。
        """
        if self._columns is None:
            return []
        problems: list[str] = []
        fact_table = self._document.fact_table
        fact_cols = self._columns.get(fact_table, frozenset())
        if self._document.date_field and self._document.date_field not in fact_cols:
            problems.append(f"date_field '{self._document.date_field}' "
                            f"不在事实表 {fact_table} 的列中")
        verified = drillable = 0
        for dim in self._document.dimensions.values():
            if dim.kind == DIM_DERIVED or not dim.table or not dim.key:
                continue   # 派生维度不 join 维度表;缺 key / table 由 validate 报
            dim_cols = self._columns.get(dim.table)
            if dim_cols is None:
                continue   # 维度表列信息未知,无法校验
            verified += 1
            if dim.key not in fact_cols:
                problems.append(f"维度 {dim.name} 的 key '{dim.key}' "
                                f"不在事实表 {fact_table} 的列中,无法 join")
            missing = [f for f in dim.hierarchy if f not in dim_cols]
            problems += [f"维度 {dim.name} 的层级字段 '{f}' 不在维度表 {dim.table} 的列中"
                         for f in missing]
            if dim.hierarchy and len(missing) == len(dim.hierarchy):
                problems.append(f"维度 {dim.name} 是孤立维度:"
                                f"层级字段 {list(dim.hierarchy)} 在表 {dim.table} 中都不存在")
            elif not missing and dim.key in fact_cols:
                drillable += 1
        if verified and not drillable:
            problems.append("没有任何维度可用于下钻:每个指标都无法归因")
        return problems

    def assert_valid(self) -> None:
        """validate() + check_reachability() 合并执行,有问题则抛 SemanticError。"""
        problems = self.validate() + self.check_reachability()
        if problems:
            raise SemanticError("语义层校验未通过:\n" + "\n".join(f"- {p}" for p in problems))

    # ------------------------------------------------------------------
    # 内部:指标引用规则(需要列信息 / 编译结果)与编译缓存
    # ------------------------------------------------------------------
    def _reference_problems(self, metric: Metric) -> list[str]:
        """指标引用的自洽性:depends_on 指向的标识符真实存在 + 与表达式实际引用一致。

        两条都要用地图之外的事实(列信息 / 编译结果),规则实现因此留在服务侧。
        """
        problems: list[str] = []
        for dep in metric.depends_on:   # 依赖的标识符既不是指标,也不是可用列
            if dep in self._document.metrics or self._columns is None:
                continue
            if dep not in self._columns_for(metric):
                problems.append(f"指标 {metric.name} 依赖的 '{dep}' 既不是指标,"
                                f"也不在表 {metric.source or self._document.fact_table} 的列中")
        if self._columns is not None and metric.name not in self._compiled.get(DEFAULT_ALIAS, {}):
            return problems + [f"指标 {metric.name} 的 expression 未能编译"]
        try:
            refs = set(referenced_identifiers(metric.expression))
        except Exception as err:   # 词法抽取的异常类型由编译器定义,一律降级为问题描述
            return problems + [f"指标 {metric.name} 的 expression 无法解析: {err}"]
        deps = set(metric.depends_on)
        if refs != deps:
            problems.append(f"指标 {metric.name} 的 depends_on {sorted(deps)} "
                            f"与表达式引用 {sorted(refs)} 不一致")
        return problems

    def _columns_for(self, metric: Metric) -> Collection[str]:
        """该指标表达式可用的列:它自己的 source 表(默认事实表)的列。"""
        if self._columns is None:
            return frozenset()
        return self._columns.get(metric.source or self._document.fact_table, frozenset())

    def _compile_all(self, alias: str) -> dict[str, str]:
        """按依赖序编译全部指标 -> {指标名: SQL};失败抛 SemanticError。"""
        cache: dict[str, str] = {}

        def compile_one(name: str) -> str:
            if name not in cache:
                metric = self._document.metrics[name]
                # 递归展开依赖:load 已拦成环,不会无限递归
                symbols = {d: compile_one(d) for d in metric.depends_on
                           if d in self._document.metrics}
                try:
                    cache[name] = compile_expression(
                        metric.expression, symbols, self._columns_for(metric), alias)
                except ExpressionError as err:
                    raise SemanticError(f"指标 {name} 的表达式无法编译: {err}") from err
            return cache[name]

        for name in self._document.metrics:
            compile_one(name)
        return cache
