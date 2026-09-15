"""模型可见 schema 与工具描述的渲染层:全部由语义层现场渲染。

从 tools.py 拆出(tools.py 有单文件行数约束):tools.py 管「工具行为与注册」,
本模块管「模型看到的形状」——参数枚举(有哪些指标 / 维度 / 层级)、字段描述、
工具 docstring。换一份语义层 = 模型看到的工具完全变了。

本模块不认识任何具体数据集(受 test_engine_has_no_hardcoded_vocabulary 扫描约束)。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from attribution.semantic import Semantic
from harness import context

# 描述里给 dims 举例时取几个层级字段:多了挤占上下文,一个不足以说明「可多字段分组」
EXAMPLE_LEVEL_COUNT = 2

# 分解声明在工具描述里的连接符号(按分解类型;structural 无数学符号,单独描述)
_KIND_JOIN = {"multiplicative": " × ", "additive": " + ", "ratio": " / "}


# ---------------------------------------------------------------------------
# 动态参数模型:枚举与描述都来自语义层
# ---------------------------------------------------------------------------
def _metric_arg(layer: Semantic) -> tuple:
    """metric 参数:值域 = 语义层的全部指标名(不在 enum 里的指标模型看不见)。"""
    return (Literal[tuple(layer.metrics)], Field(description="指标名,取值见下方可用指标"))


def _dimension_arg(layer: Semantic) -> tuple:
    """dimension 参数:值域 = 语义层的全部维度名。"""
    return (Literal[tuple(layer.dimensions)], Field(description="下钻哪个维度,取值见下方维度清单"))


def _level_arg(layer: Semantic) -> tuple:
    """level 参数:逐维度列出可下钻层级(写死层级会让模型下钻到不存在的一层)。"""
    return (str, Field(description="层级字段,必须是所选维度层级里的一个:" + _level_catalog(layer)))


def _level_catalog(layer: Semantic) -> str:
    """各维度可下钻的层级字段,形如「维度 -> 层1/层2/层3」,由语义层 hierarchy 渲染。"""
    return ";".join(f"{d.name} -> {'/'.join(d.hierarchy)}" for d in layer.dimensions.values())


def _example_of(iterable, attr: str) -> str:
    """取第一个元素上某个属性的取值,没有则返回空串(示例必须来自语义层,不能写死)。"""
    for item in iterable:
        value = getattr(item, attr, "")
        if value:
            return str(value)
    return ""


def _dims_example(layer: Semantic) -> str:
    """dims 该填什么:用语义层里真实存在的层级字段举例。"""
    levels = [d.hierarchy[-1] for d in layer.dimensions.values() if d.hierarchy]
    return "[" + ", ".join(f"'{name}'" for name in levels[:EXAMPLE_LEVEL_COUNT]) + "]"


def _filters_example(layer: Semantic) -> str:
    """filters 该填什么:用语义层里真实存在的主键字段举例。"""
    key = _example_of(layer.dimensions.values(), "key")
    return f"{{'{key}': '<该切片的取值>'}}" if key else "{}"


def _decompose_target_arg(layer: Semantic) -> tuple:
    """decompose 的 target 参数:值域 = 语义层分解声明的 target 集合;无声明时退化为文本
    (引擎会如实抛 SemanticError,好过工具构建时报错)。"""
    targets = tuple(d.target for d in layer.decompositions)
    if targets:
        return (Literal[targets], Field(description="要分解的指标,取值见下方分解声明"))
    return (str, Field(description="要分解的指标(本语义层没有声明任何分解)"))


# ---------------------------------------------------------------------------
# 工具描述:同样由语义层渲染(指标清单 / 维度层级 / 过滤规则都不写死)
# ---------------------------------------------------------------------------
def _overview_doc() -> str:
    return ("返回语义层目录:可用指标、维度、每个维度的下钻层级。\n"
            "分析前先调用它,了解有哪些指标与维度可查、每个维度能下钻到哪几层,\n"
            "避免凭空猜测字段名。返回一段纯文本摘要。")


def _anomaly_doc(layer: Semantic) -> str:
    return "\n".join([
        "检测某指标在 [start, end] 是否发生显著异常(日历感知,无需指定基期)。",
        "基线取窗口之前近 4 周「同星期几」的中位数(促销日历内的样本已剔除),",
        "对比窗口内日值的中位数;命中促销日历时 is_expected=True(属预期脉冲,不是业务异常)。",
        "返回 is_anomaly / is_expected / change_rate / anomaly_kind / baseline_type / note。",
        "先用它判断「是不是真的有异常」,再决定要不要下钻。时间参数用 YYYY-MM-DD。",
        "",
        context.render_metric_catalog(layer),
    ])


def _contribute_doc(layer: Semantic) -> str:
    return "\n".join([
        "做维度下钻,找出「哪些切片最拖累(或最拉动)指标变化」。",
        "对比基准期 [base_start, base_end] 与对比期 [cmp_start, cmp_end],返回按变化",
        "贡献度排序的前 top_k 个切片(最拖累的在前)。这是定位根因的核心工具:",
        "发现某个切片异常后,用它下钻到更细一层。时间参数用 YYYY-MM-DD。",
        "",
        context.render_metric_catalog(layer),
        "",
        context.render_dimension_catalog(layer),
        "",
        f"level: 必须是 dimension 对应维度层级里的一个,例如 {_level_catalog(layer)}。",
        "派生指标的贡献度是 null,看 change_rate 判断。",
        "",
        context.render_filter_rules(layer),
    ])


def _query_doc(layer: Semantic) -> str:
    return "\n".join([
        "查询某指标在 [start, end] 时间范围内的聚合值。",
        "",
        context.render_metric_catalog(layer),
        "",
        context.render_dimension_catalog(layer),
        "",
        f"dims: 要分组的维度层级字段列表,例如 {_dims_example(layer)};空列表表示不分组取总量。",
        f"filters: 切片过滤字典,例如 {_filters_example(layer)};无过滤传 {{}}。",
        "start/end: 'YYYY-MM-DD'。",
        "",
        context.render_filter_rules(layer),
    ])


def _decompose_doc(layer: Semantic) -> str:
    """decompose 工具说明:分解声明清单由语义层渲染,逐条给出数学形态。"""
    lines = [_decl_line(d) for d in layer.decompositions] or [
        "  - (本语义层没有声明任何分解)"]
    return "\n".join([
        "对某指标做量级分解:把基期→对比期的总变化拆成各因子的效应,效应之和等于总变化",
        "(零残差)。能回答「下滑是量掉了还是价掉了」「是结构变了还是自身水平变了」这类问题——",
        "它是探究机制的工具,和 contribute 的「谁拖累的」互补。时间参数用 YYYY-MM-DD。",
        "结构分解会把「下架/新上」实体(任一期分母为 0)单列进 entity_changes,不进 effects;",
        "找根因实体(如下架 SKU)要看 entity_changes 而不是 effects。",
        "已声明的分解组合:",
        *lines,
        "",
        "dimension/level: 不传则对整窗分解一次;传了则按该维度切片、各分解一次取前 top_k",
        f"(level 须在该维度层级里:{_level_catalog(layer)})。filters 与其他工具同口径。",
    ])


def _decl_line(decl) -> str:
    """一条分解声明的数学形态:符号按 kind 取,structural 无符号、标注实体维度与 entity_changes。"""
    sep = _KIND_JOIN.get(decl.kind)
    if sep:
        return f"  - {decl.target} = {sep.join(decl.factors)}"
    return (f"  - {decl.target} = 结构效应 + 自身效应(实体维度:{decl.entity_dimension};"
            f"下架/新上实体在 entity_changes)")