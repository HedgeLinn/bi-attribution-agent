"""分解声明(§4.2)与日历节点的定义、解析与纯规则。

独立成模块的唯一原因:semantic_schema / semantic 已到单文件行数上限(≤300)。
分层不变:本模块属于「地图层」——只读 YAML 原始节点、出定义实体与问题描述,
**不编译 SQL、不碰 DuckDB、不认识引擎**。

依赖方向(避免环):semantic_schema -> 本模块(取 Decomposition 与解析函数);
本模块不反向 import semantic_schema 的任何名字——需要 SemanticError 时在
函数内惰性导入(冷路径,只发生在结构错误 / 匹配失败时)。

受 test_engine_has_no_hardcoded_vocabulary 的 AST 扫描约束:可执行代码里
不得出现数据集词汇,docstring 与注释里的举例除外。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "Decomposition", "find_decomposition_problems",
    "match_decomposition", "parse_calendar", "parse_decompositions",
]

# 分解声明(decompositions.kind)的合法取值(§4.2;与 attribution/decompose 的 KIND_* 一一对应。
# 地图层不 import 算法层——这里是取值词汇的唯一事实来源,算法层在自己的常量里对齐)
_KIND_MULTIPLICATIVE = "multiplicative"
_KIND_ADDITIVE = "additive"
_KIND_RATIO = "ratio"
_KIND_STRUCTURAL = "structural"
_KNOWN_DECOMP_KINDS = frozenset({_KIND_MULTIPLICATIVE, _KIND_ADDITIVE,
                                 _KIND_RATIO, _KIND_STRUCTURAL})
_MIN_MULTIPLICATIVE_FACTORS = 2   # 乘法分解:少于两个因子谈不上「分解」
_MIN_ADDITIVE_FACTORS = 1         # 加法分解:至少一个分项
_RATIO_FACTORS = 2                # 比率分解:恰两个因子(分子在前、分母在后)


@dataclass(frozen=True)
class Decomposition:
    """语义层 decompositions 里的一条声明(§3.2 / §4.2):target 可沿哪些因子分解。

    - multiplicative / additive / ratio:factors 是**指标名**(因子值 = 指标窗口聚合)
    - structural:factors 是**人为命名的两类效应**(如 product_mix / price),
      真正的取值口径(权重 / 强度)由引擎按 entity_dimension 决定(§4.3)
    """

    target: str
    kind: str
    factors: tuple[str, ...]
    entity_dimension: str | None = None   # 仅 structural:实体来自哪个维度


def _opt_str(value: object) -> str | None:
    """去空白字符串;None / 空串 -> None。"""
    text = None if value is None else str(value).strip()
    return text or None


def _str_list(entry: Mapping, key: str) -> tuple[str, ...]:
    """取字符串列表字段(缺省 -> 空元组;写成标量属结构错误)。

    与 semantic_schema._str_list 同构的小工具:本模块不反向 import semantic_schema,
    故保留一份本地实现(6 行,换取依赖单向)。
    """
    value = entry.get(key) or ()
    if not isinstance(value, (list, tuple)):
        from attribution.semantic_schema import SemanticError
        raise SemanticError(f"{key} 必须是列表,得到 {value!r}")
    return tuple(str(item) for item in value)


def parse_decompositions(raw: Mapping) -> tuple[Decomposition, ...]:
    """解析 decompositions 节点 -> 声明元组;缺省空元组,非列表属结构错误。"""
    value = raw.get("decompositions") or []
    if not isinstance(value, list):
        from attribution.semantic_schema import SemanticError
        raise SemanticError("decompositions 必须是列表")
    return tuple(_build_decomposition(i, entry) for i, entry in enumerate(value))


def parse_calendar(raw: Mapping) -> Mapping[str, object]:
    """time.calendar 节点原样保留:形状自由(日历种类 -> 条目列表),由消费方校验(§4.4)。"""
    time_block = raw.get("time")
    if not isinstance(time_block, Mapping):
        return {}
    calendar = time_block.get("calendar")
    return calendar if isinstance(calendar, Mapping) else {}


def _build_decomposition(index: int, entry: object) -> Decomposition:
    """由 YAML 条目构造 Decomposition;条目不是映射属结构错误。

    缺 target / kind 不算结构错误(留空交由校验报问题)——semantic_diff 的
    「实例输入」路径必须能从**不完整**的语义层构造出 Semantic(diff 在原始节点上
    作业,层里可能有半成品条目),fail-fast 会堵死这条路。
    """
    if not isinstance(entry, Mapping):
        from attribution.semantic_schema import SemanticError
        raise SemanticError(f"decompositions[{index}] 必须是映射")
    return Decomposition(
        target=_opt_str(entry.get("target")) or "",
        kind=_opt_str(entry.get("kind")) or "",
        factors=_str_list(entry, "factors"),
        entity_dimension=_opt_str(entry.get("entity_dimension")),
    )


def match_decomposition(decompositions: Sequence[Decomposition], target: str,
                        factors: Collection[str]) -> Decomposition:
    """查 (target, factors) 的分解声明;因子集合一致即命中(顺序任意)。

    未命中抛 SemanticError,并在消息里列出该 target 的全部合法 factors。
    kind 与 entity_dimension 一律以声明为准——分解必须走「地图」,不许现编。
    """
    wanted = frozenset(factors)
    candidates = [decl for decl in decompositions if decl.target == target]
    for decl in candidates:
        if frozenset(decl.factors) == wanted:
            return decl
    from attribution.semantic_schema import SemanticError
    options = [f"{list(d.factors)}[{d.kind}]" for d in candidates]
    raise SemanticError(
        f"语义层没有声明指标 {target} 按 {sorted(wanted)} 的分解"
        + (f";已声明的组合: {' / '.join(options)}" if options
           else ";" + "该指标没有任何分解声明")
    )


def find_decomposition_problems(
    decompositions: Sequence[Decomposition],
    metrics: Mapping[str, object],
    dimensions: Mapping[str, object],
) -> list[str]:
    """分解声明的自洽性问题(§4.2)。按声明顺序报全;只看地图就能判定。

    - kind 必须合法;target 必须是已有指标
    - factors 数量约束:multiplicative >= 2 / additive >= 1 / ratio == 2
    - multiplicative / additive / ratio:factors 必须是**指标名**(因子值 = 指标聚合)
    - structural:entity_dimension 必须声明且是已有维度;factors 是人为命名,不查指标

    入参用 Mapping[str, object] 而非 Metric / Dimension:本模块不反向 import
    semantic_schema,「名字在不在映射里」用键判断就够(实体类型由 schema 层保证)。
    """
    problems: list[str] = []
    for i, decl in enumerate(decompositions):
        tag = f"decompositions[{i}](target={decl.target})"
        if decl.kind not in _KNOWN_DECOMP_KINDS:
            problems.append(f"{tag} 的 kind '{decl.kind}' 未知")
            continue
        if decl.target not in metrics:
            problems.append(f"{tag} 的 target 不是已有指标")
            continue
        n_factors = len(decl.factors)
        if (decl.kind == _KIND_MULTIPLICATIVE and n_factors < _MIN_MULTIPLICATIVE_FACTORS
                or decl.kind == _KIND_ADDITIVE and n_factors < _MIN_ADDITIVE_FACTORS
                or decl.kind == _KIND_RATIO and n_factors != _RATIO_FACTORS):
            problems.append(f"{tag} 的 factors 数量 {n_factors} 不符合 {decl.kind} 的要求")
        if decl.kind == _KIND_STRUCTURAL:
            if not decl.entity_dimension:
                problems.append(f"{tag} 是 structural,必须声明 entity_dimension")
            elif decl.entity_dimension not in dimensions:
                problems.append(f"{tag} 的 entity_dimension '{decl.entity_dimension}' 不是已有维度")
        else:
            unknown = [f for f in decl.factors if f not in metrics]
            if unknown:
                problems.append(f"{tag} 的因子不是已有指标: {', '.join(unknown)}")
    return problems
