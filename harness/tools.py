"""harness 的 Tool 组件:把归因引擎包装成模型可调用的工具。

**schema 由语义层生成**(docs/REUSE_DESIGN.md §3.4):参数的可选值域(有哪些指标、
哪些维度、哪些下钻层级)与工具描述里的指标清单/层级清单,全部从语义层渲染。
所以「换一份语义层 = 模型看到的工具完全变了」——只改 YAML 也能生效。
改造前这里的枚举是写死的 `Literal[...]`,新维度在模型眼里根本不存在。

分层:本模块只做「参数校验 + 转发」,不做任何业务计算(计算在 attribution/);
本模块不认识任何具体数据集——出现数据集里的词(§3.6⑤ 那份词汇表)即为回退。
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import Field, create_model

from attribution.engine import AttributionEngine
from attribution.semantic import Semantic, SemanticError
from harness import context, tool_catalog

# 工具名:harness 对模型的稳定契约,与语义层无关(改名等于换接口)
OVERVIEW_TOOL = "get_semantic_overview"
ANOMALY_TOOL = "detect_anomaly"
CONTRIBUTE_TOOL = "contribute"
QUERY_TOOL = "query_metric"
DECOMPOSE_TOOL = "decompose"

DEFAULT_TOP_K = 5            # 下钻返回的切片条数默认值(与引擎默认值一致)
DEFAULT_THRESHOLD = 0.15     # 异常判定阈值默认值(与引擎默认值一致)

# 探测数据时间范围用的哨兵窗口:远早于 / 远晚于任何真实数据,只为取到时间字段的极值。
# 用固定常量而不是「今天 ± N 年」,是为了让探测结果可复现(不随运行日期漂移)。
PROBE_WINDOW_START = "1900-01-01"
PROBE_WINDOW_END = "2999-12-31"

# 描述里给 dims 举例时取几个层级字段:多了挤占上下文,一个不足以说明「可多字段分组」
EXAMPLE_LEVEL_COUNT = 2

# 全局单例,由 init_engine 注入;重新调用即切换数据集(工具与提示词随之重建)
_engine: AttributionEngine | None = None
_semantic: Semantic | None = None
_dataset_context: context.DatasetContext | None = None


def init_engine(data_dir: str, semantic_path: str) -> None:
    """初始化(或切换)数据集:引擎 + 语义层 + 数据集上下文。

    语义层实例直接复用引擎已加载的那一份(含列信息与编译缓存),不再二次解析 YAML。
    """
    global _engine, _semantic, _dataset_context
    engine = AttributionEngine(data_dir=data_dir, semantic_path=semantic_path)
    _engine = engine
    _semantic = engine.semantic
    _dataset_context = context.DatasetContext.load(semantic_path).with_date_range(
        _probe_date_range(engine, _semantic)
    )


def _e() -> AttributionEngine:
    if _engine is None:
        raise RuntimeError("AttributionEngine 未初始化")
    return _engine


def semantic() -> Semantic:
    """当前语义层(供 loop 渲染系统提示词;未初始化时与引擎一样直接报错)。"""
    if _semantic is None:
        raise RuntimeError("语义层未初始化")
    return _semantic


def dataset_context() -> context.DatasetContext:
    """当前数据集上下文(促销日历 / 口径陷阱 / 数据时间范围)。"""
    if _dataset_context is None:
        raise RuntimeError("数据集上下文未初始化")
    return _dataset_context


def _probe_date_range(engine: AttributionEngine, layer: Semantic) -> tuple[str, str] | None:
    """从数据探测时间字段的取值区间(不写死日期范围,换数据集后自动跟着变)。

    走引擎的公共查询接口:用任一指标按时间字段分组,分组键就是数据里真实出现过的
    取值,取两端即得区间。探测失败返回 None——提示词里不写时间范围,也好过编一个假的。
    """
    field, names = layer.date_field, list(layer.metrics)
    if not field or not names:
        return None
    try:
        rows = engine.query_metric(names[0], [field], {}, PROBE_WINDOW_START, PROBE_WINDOW_END)
    except Exception:  # noqa: BLE001  数据缺失 / 字段不可分组:探测失败不该拖垮初始化
        return None
    values = sorted(str(row[field]) for row in rows.get("rows", []) if row.get(field) is not None)
    return (values[0], values[-1]) if values else None


# ---------------------------------------------------------------------------
# 工具实现:只做转发,参数校验交给动态 schema(pydantic 模型)
# ---------------------------------------------------------------------------
def _query(metric: str, dims: list[str], filters: dict, start: str, end: str) -> dict:
    return _e().query_metric(metric, dims, filters, start, end)


def _contribute(metric: str, dimension: str, level: str, base_start: str, base_end: str,
                cmp_start: str, cmp_end: str, top_k: int = DEFAULT_TOP_K,
                filters: dict | None = None) -> dict:
    return _e().contribute(metric, dimension, level,
                           base_start, base_end, cmp_start, cmp_end, top_k, filters)


def _anomaly(metric: str, start: str, end: str, threshold: float = DEFAULT_THRESHOLD,
             cmp_start: str | None = None, cmp_end: str | None = None) -> dict:
    # cmp_start/cmp_end 已弃用(契约 v2:基线由窗口之前的历史估计,不再由调用方指定基期);
    # 保留参数仅为旧调用兼容,未传时以 start/end 占位——引擎根本不消费它们。
    return _e().detect_anomaly(metric, start, end, cmp_start or start, cmp_end or end, threshold)


def _decompose(target: str, base_start: str, base_end: str, cmp_start: str, cmp_end: str,
               dimension: str | None = None, level: str | None = None,
               filters: dict | None = None, top_k: int = DEFAULT_TOP_K) -> dict:
    # factors 不暴露给模型:从语义层声明解析——(target, factors) 必须命中声明才合法,
    # 由工具代填可以杜绝模型拼错因子组合(SemanticError 只会来自「target 无声明」)。
    layer = _e().semantic
    decl = next((d for d in layer.decompositions if d.target == target), None)
    if decl is None:
        raise SemanticError(f"语义层没有指标 {target} 的分解声明")
    return _e().decompose(target, list(decl.factors), base_start, base_end, cmp_start,
                          cmp_end, dimension=dimension, level=level, filters=filters,
                          top_k=top_k)


# 参数模型与工具描述都在 harness.tool_catalog(由语义层现场渲染;本文件有行数约束)


# ---------------------------------------------------------------------------
# 工具组装
# ---------------------------------------------------------------------------
def _overview_tool(layer: Semantic) -> StructuredTool:
    """概览工具:无需参数;正文委托 Semantic.render_overview(),不在 harness 里重复渲染。"""
    return StructuredTool.from_function(
        func=layer.render_overview, name=OVERVIEW_TOOL, description=tool_catalog._overview_doc(),
        args_schema=create_model("SemanticOverviewArgs"),
    )


def _anomaly_tool(layer: Semantic) -> StructuredTool:
    args = create_model(
        "DetectAnomalyArgs",
        metric=tool_catalog._metric_arg(layer),
        start=(str, Field(description="对比期起始日期 YYYY-MM-DD")),
        end=(str, Field(description="对比期结束日期 YYYY-MM-DD")),
        threshold=(float, Field(default=DEFAULT_THRESHOLD, description="异常判定阈值(变化率绝对值)")),
        cmp_start=(str | None, Field(default=None, description="已弃用,无需填写(基线由窗口之前的历史估计)")),
        cmp_end=(str | None, Field(default=None, description="已弃用,无需填写(基线由窗口之前的历史估计)")),
    )
    return StructuredTool.from_function(
        func=_anomaly, name=ANOMALY_TOOL, description=tool_catalog._anomaly_doc(layer),
        args_schema=args,
    )


def _contribute_tool(layer: Semantic) -> StructuredTool:
    args = create_model(
        "ContributeArgs",
        metric=tool_catalog._metric_arg(layer),
        dimension=tool_catalog._dimension_arg(layer),
        level=tool_catalog._level_arg(layer),
        base_start=(str, Field(description="基准期起始日期 YYYY-MM-DD")),
        base_end=(str, Field(description="基准期结束日期 YYYY-MM-DD")),
        cmp_start=(str, Field(description="对比期起始日期 YYYY-MM-DD")),
        cmp_end=(str, Field(description="对比期结束日期 YYYY-MM-DD")),
        top_k=(int, Field(default=DEFAULT_TOP_K, description="返回的切片条数")),
        filters=(dict, Field(default_factory=dict, description="切片过滤字典,无过滤传 {}")),
    )
    return StructuredTool.from_function(
        func=_contribute, name=CONTRIBUTE_TOOL, description=tool_catalog._contribute_doc(layer),
        args_schema=args,
    )


def _query_tool(layer: Semantic) -> StructuredTool:
    args = create_model(
        "QueryMetricArgs",
        metric=tool_catalog._metric_arg(layer),
        dims=(list[str], Field(description="分组用的维度层级字段列表,空列表表示不分组")),
        filters=(dict, Field(default_factory=dict, description="切片过滤字典,无过滤传 {}")),
        start=(str, Field(description="起始日期 YYYY-MM-DD")),
        end=(str, Field(description="结束日期 YYYY-MM-DD")),
    )
    return StructuredTool.from_function(
        func=_query, name=QUERY_TOOL, description=tool_catalog._query_doc(layer), args_schema=args,
    )


def _decompose_tool(layer: Semantic) -> StructuredTool:
    args = create_model(
        "DecomposeArgs",
        target=tool_catalog._decompose_target_arg(layer),
        base_start=(str, Field(description="基准期起始日期 YYYY-MM-DD")),
        base_end=(str, Field(description="基准期结束日期 YYYY-MM-DD")),
        cmp_start=(str, Field(description="对比期起始日期 YYYY-MM-DD")),
        cmp_end=(str, Field(description="对比期结束日期 YYYY-MM-DD")),
        dimension=(str | None, Field(default=None, description="切片分解的维度名;不传则整窗分解")),
        level=(str | None, Field(default=None, description="层级字段,必须与 dimension 同时传")),
        filters=(dict, Field(default_factory=dict, description="切片过滤字典,无过滤传 {}")),
        top_k=(int, Field(default=DEFAULT_TOP_K, description="切片形态下返回的切片条数")),
    )
    return StructuredTool.from_function(
        func=_decompose, name=DECOMPOSE_TOOL, description=tool_catalog._decompose_doc(layer),
        args_schema=args,
    )


def build_tools(layer: Semantic | None = None) -> list[StructuredTool]:
    """按语义层**现场生成**模型可见的工具集(默认用 init_engine 注入的那一份)。

    每次调用都重新建模:切换语义层后,枚举与描述随之改变(§3.7 前端切语义的前提)。
    """
    target = layer if layer is not None else semantic()
    return [_overview_tool(target), _anomaly_tool(target),
            _contribute_tool(target), _query_tool(target), _decompose_tool(target)]
