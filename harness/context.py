"""提示词的**两段式**渲染:数据集无关的方法论 + 由语义层渲染的数据集上下文。

契约:docs/REUSE_DESIGN.md §3.4(工具与提示词动态化)/ §3.7(前端切语义)

**为什么要拆两段**:
    方法论(假设-验证循环、下钻纪律、「整体下跌」不算结论、业务问题 vs 数据问题)
    对任何数据集都成立,是不变的**常量**;数据集上下文(指标清单、维度与下钻层级、
    数据的时间范围、促销日历、口径陷阱)只对当前这张语义层成立,必须**渲染**。
    两者混在一处的后果是换一套数据后提示词仍在误导模型——改造前 loop.py 把日期
    范围写死在提示词里,换数据集后模型会去查一个根本不存在的区间。

边界:
    - 本模块只渲染文本:**不碰 LLM、不碰 DuckDB、不做业务计算**
    - 时间范围由调用方从数据探测后传入,本模块不自己去查(探测要用引擎)
    - 本模块不认识任何具体数据集:出现的每一个数据集词汇都来自语义层(§3.6⑤)
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from attribution.annotations import Annotation, load_annotations, select_relevant
from attribution.semantic import Semantic, load_yaml
from harness.datasets import DEFAULT_DATASETS_DIRNAME

__all__ = [
    "CalendarEntry", "DatasetContext", "render_dataset_context", "render_dimension_catalog",
    "render_filter_rules", "render_metric_catalog", "render_system_prompt",
]

# 归因结论沉淀(§6.3):单次注入 3 条、每条 ≤240 字(历史只是起点,不该挤掉本次分析)。
ANNOTATION_TOP_N = 3
ANNOTATION_CHARS = 240

# 数据集无关的方法论:假设-验证循环、下钻纪律、业务问题 vs 数据问题、输出格式。
# 换数据集时这段**一个字都不用改**——凡是需要跟着数据变的,都不该写在这里。
_METHODOLOGY = """你是一个归因分析师,通过「指标变化 → 维度下钻 → 根因定位」回答业务问题。

## 你的工作方式(假设-验证循环)
1. 先判断异常:用 detect_anomaly 确认指标是否真的发生了显著变化,再决定要不要下钻。
2. 再提出假设:变化可能由哪些维度切片导致。这是你的分析价值所在,不要跳过这一步。
3. 用 contribute 逐层下钻:先在粗层看哪些切片贡献度最大,再对可疑切片下钻到更细一层,
   直到定位到最细的切片为止。
4. 交叉验证:单一维度下钻可能不够,用 query_metric 对可疑切片查派生指标,区分
   「是量变了、价变了、还是比率类指标异常」,这往往是根因的真正线索。
5. 机制分解:contribute 定位「谁拖累的」,decompose 解释「为什么」——把指标变化按因子拆开(量变/价变/结构变),各因子效应之和等于总变化。

## 关键规则
- 下钻前先用 get_semantic_overview 了解有哪些指标、维度、每个维度能下钻到哪几层,
  不要凭空猜测指标名与字段名。
- contribute 的 level 必须是该维度层级里真实存在的一层;先在粗层定位,再逐层往下。
- contribute 返回的每个切片带 key(该切片的原始标识)与 label(显示名)。用 query_metric
  的 filters 过滤时,主键层字段必须用 key 值,不要用 label 显示名。
- 贡献度下钻只能回答「哪个切片拖累的」;要解释机制(量变/价变/结构变)时用 decompose
  做因子分解,效应之和等于总变化(零残差),与 contribute 互补。
- 派生指标的贡献度是 null,改用 change_rate 判断它是否异常。
- 不要只停在「整体下跌」就下结论:整体回落可能是背景噪声,必须下钻到最细一层,
  才能确认真正的异常点。
- 命中促销日历的回落属于预期脉冲,不是异常,不要当成业务问题上报。
- 时间参数一律用 YYYY-MM-DD 格式。

## 区分「业务问题」与「数据问题」
- 业务问题:数据本身完整,变化由真实业务动作导致(某个切片确实异常)。
- 数据问题:发现指标缺数、口径不一致、字段整列为空、量级明显失真、层级断裂等,
  必须**报告「这是数据质量问题」**,指出可疑字段与范围(例:某段时间的取值为空)。
  不要硬编一个业务原因,也不要用一个看似合理的业务解释掩盖数据缺陷。
- 两种结论都要给证据链:哪一步、用了哪个工具、看到了什么客观数字。

## 最终输出格式
当分析完成,停止调用工具,输出一个 JSON(不要用 markdown 代码块包裹,直接输出 JSON):
{
  "结论": "一句话根因结论(若属数据质量问题,明确写成数据质量问题)",
  "根因": {"dimension": "...", "level": "...", "key": "...", "label": "..."},
  "量级": {"贡献度": 0.78, "变化量": -123456},
  "证据链": ["每一步发现的客观事实", "..."],
  "已排除": ["你实际用证据排除过的假设", "..."],
  "无法归因": false,
  "置信度": "high",
  "建议": "可执行的下一步动作"
}

- 「根因」必须是工具**实际返回过的**切片:dimension/level/key 三者与工具结果对齐,
  不许编造;label 写该切片的显示名。定位不到根因时「无法归因」写 true、「根因」写 null。
- 「量级.贡献度」取你对该切片下钻时工具给出的贡献度数值,没有就写 null。
- 「已排除」列出你在分析过程中考虑过、且用客观证据否掉的假设(会被机械核对)。
- 「置信度」取值只能是 high / medium / low:证据链完整且数值一致才是 high。
"""


@dataclass(frozen=True)
class CalendarEntry:
    """一条活动/大促日历条目:名字 + 起止 + 备注。"""

    name: str
    start: str = ""
    end: str = ""
    note: str = ""


@dataclass(frozen=True)
class DatasetContext:
    """数据集上下文里**不属于「指标/维度」地图结构**的那部分事实。

    口径陷阱与促销日历写在语义层 YAML 的文档级节点里,时间范围只能从数据里探测,
    三者都进提示词,也都不该被写死在 harness 里。

    时间范围取不到时保持 None:提示词里相应段落整体消失,而不是编一个假的区间。
    """

    caveats: tuple[str, ...] = ()
    calendar: tuple[CalendarEntry, ...] = ()
    date_range: tuple[str, str] | None = None

    @classmethod
    def load(cls, semantic_path: str | Path) -> "DatasetContext":
        """从语义层 YAML 读取文档级事实(口径陷阱 / 促销日历)。

        复用 attribution.semantic 的加载器:与语义层本体同一套 fail-fast 规则,
        harness 不另开一个 YAML 读取入口(§3.4:三处读取方要收敛)。
        """
        raw = load_yaml(str(semantic_path))
        if not isinstance(raw, Mapping):
            return cls()
        return cls(caveats=_read_caveats(raw), calendar=_read_calendar(raw))

    def with_date_range(self, span: tuple[str, str] | None) -> "DatasetContext":
        """返回带上时间范围的新实例(frozen:不改原对象,便于测试与切换数据集)。"""
        return replace(self, date_range=span)


def render_system_prompt(semantic: Semantic, dataset_context: DatasetContext | None = None,
                         query: str | None = None) -> str:
    """渲染系统提示词 = 数据集无关的方法论(常量) + 数据集上下文(由语义层渲染)。

    签名演进(M6,§6.3):新增可选参数 query。不给时输出与加这个参数之前**逐字相同**。
    """
    prompt = f"{_METHODOLOGY}\n{render_dataset_context(semantic, dataset_context)}"
    history = _annotations_block(semantic, query)
    return f"{prompt}\n\n{history}" if history else prompt


def render_dataset_context(semantic: Semantic, dataset_context: DatasetContext | None = None) -> str:
    """渲染数据集上下文:指标清单、维度下钻层级、数据时间范围、促销日历、口径陷阱。"""
    facts = dataset_context or DatasetContext()
    blocks = [
        _dataset_block(semantic),
        render_metric_catalog(semantic),
        render_dimension_catalog(semantic),
        _time_span_block(facts),
        _calendar_block(facts),
        _caveats_block(facts),
    ]
    return "\n\n".join(block for block in blocks if block)


def render_metric_catalog(semantic: Semantic) -> str:
    """指标清单(名字 / 展示名 / 口径表达式 / 类型 / 时间聚合 / 单位),提示词与工具描述共用。

    单位必须随目录一起送出去:同一 agent 跨数据集会拿到不同量纲的数值(如元 vs 分),
    声明了单位却不渲染,模型会把 19527300 分当成 19527300 元——差 100 倍。
    """
    lines = [f"  - {m.name}({m.label}): {m.expression} "
             f"[{m.type} · 时间聚合 {m.time_aggregation}"
             f"{' · 单位 ' + m.unit if m.unit else ''}]"
             for m in semantic.metrics.values()]
    return "\n".join(["## 可用指标", *lines])


def render_dimension_catalog(semantic: Semantic) -> str:
    """维度与下钻层级(从粗到细),提示词与工具描述共用。"""
    lines = [f"  - {d.name}({d.label}): {' -> '.join(d.hierarchy)}"
             for d in semantic.dimensions.values()]
    return "\n".join(["## 维度与下钻层级(从左到右由粗到细)", *lines])


def render_filter_rules(semantic: Semantic) -> str:
    """渲染「过滤值怎么取」的规则:主键字段用 ID,展示名列不能当过滤值。

    规则本身数据集无关,但**字段名必须来自语义层**——写死字段名等于把数据集知识
    搬进 harness,换个数据集就会误导模型。
    """
    keys = [f"{d.key}({d.label})" for d in semantic.dimensions.values() if d.key]
    shown = [d.name_column for d in semantic.dimensions.values() if d.name_column]
    lines = ["过滤规则:", "  - filters 的键必须是某个维度的层级字段,值必须是该字段的原始取值。"]
    if keys:
        lines.append(f"  - 主键字段({'、'.join(keys)})用 key(ID 本身),不要用它的显示名。")
    if shown:
        lines.append(f"  - 展示名列({'、'.join(shown)})只用于阅读,不能作为过滤值。")
    lines.append("  - 其余层级字段用层级值本身;拿不准取值时,先查出该切片的 key 再过滤。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 以下为内部实现:文档级事实的读取与各段落的渲染
# ---------------------------------------------------------------------------
def _read_caveats(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """口径陷阱:数据本身不可比之处。不提前说明,模型会把数据问题当业务问题。"""
    entries = raw.get("caveats")
    if not isinstance(entries, Sequence) or isinstance(entries, str):
        return ()
    return tuple(text for text in (str(item).strip() for item in entries) if text)


def _read_calendar(raw: Mapping[str, Any]) -> tuple[CalendarEntry, ...]:
    """促销日历:通用遍历 time.calendar 下的每个日历。

    刻意不写死任何日历名——语义层加了新日历,提示词自动带上,harness 不用改一行。
    """
    time_block = _as_mapping(raw.get("time"))
    calendar = _as_mapping(time_block.get("calendar"))
    entries_found: list[CalendarEntry] = []
    for calendar_name, entries in calendar.items():
        if not isinstance(entries, Sequence) or isinstance(entries, str):
            continue
        entries_found += [_entry_from(calendar_name, entry) for entry in entries
                          if isinstance(entry, Mapping)]
    return tuple(entries_found)


def _entry_from(calendar_name: Any, entry: Mapping[str, Any]) -> CalendarEntry:
    """一条日历条目 -> CalendarEntry;范围与备注缺省时留空,不虚构取值。"""
    span = [str(value) for value in _as_sequence(entry.get("range"))]
    return CalendarEntry(name=str(entry.get("name") or calendar_name),
                         start=span[0] if span else "", end=span[-1] if len(span) > 1 else "",
                         note=str(entry.get("note") or ""))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """YAML 节点统一成 mapping;不是映射时退化,由调用方决定怎么处理。"""
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> list[Any]:
    """YAML 节点统一成列表;标量/None 退化为空列表。"""
    if isinstance(value, Sequence) and not isinstance(value, str):
        return list(value)
    return []


def _dataset_block(semantic: Semantic) -> str:
    """数据集标识:提示词开头点明「下面这些事实属于哪个数据集」。"""
    return f"# 当前数据集:{semantic.dataset}" if semantic.dataset else ""


def _time_span_block(facts: DatasetContext) -> str:
    """数据的时间范围(由调用方从数据探测):换数据集后自动跟着变,不写死。"""
    if not facts.date_range or len(facts.date_range) != 2:
        return ""
    return f"## 数据的时间范围\n  数据中的时间取值在 {facts.date_range[0]} ~ {facts.date_range[1]} 之间。"


def _calendar_block(facts: DatasetContext) -> str:
    """促销日历:命中促销期的「异常」往往是预期脉冲,不该报为业务问题(§4.4)。"""
    if not facts.calendar:
        return ""
    lines = [f"  - {entry.name}: {entry.start} ~ {entry.end}"
             + (f"({entry.note})" if entry.note else "")
             for entry in facts.calendar]
    return "\n".join(["## 促销日历", *lines])


def _caveats_block(facts: DatasetContext) -> str:
    """口径陷阱:数据本身不可比之处,提前告诉模型,免得它硬编业务原因。"""
    if not facts.caveats:
        return ""
    return "\n".join(["## 口径陷阱", *(f"  - {caveat}" for caveat in facts.caveats)])


# 以下为「历史归因结论」段(§6.3):数据集过往结论的选择性回注
def _annotations_block(semantic: Semantic, query: str | None) -> str:
    """按相关性取 top-n 条历史结论,渲染成「时间 / 原问题 / 要点 / 已排除假设」段落。

    沉淀文件在数据集目录下(datasets/<id>/annotations.jsonl,与 harness.datasets 同布局);
    没给问题、没有文件、文件有坏行、选不出相关项,都安静地不注入——沉淀是锦上添花。
    """
    if not query:
        return ""
    target = Path(DEFAULT_DATASETS_DIRNAME) / semantic.dataset / "annotations.jsonl"
    try:
        picked = select_relevant(load_annotations(str(target)), query, ANNOTATION_TOP_N)
    except Exception:  # noqa: BLE001  见 docstring:沉淀的读写问题不许拖垮主流程
        return ""
    lines = ["## 历史归因结论(与本问题相关的过往分析,仅供参考)",
             "  这些是提问的起点,不是证据:必须在当前数据上用工具重新验证,不许当结论照抄。"]
    for item in picked:
        lines.append(f"  - [{item.ts}] {item.query}")
        if item.confirmed:
            lines.append(f"      结论要点: {_summary_text(item.confirmed)}")
        if item.ruled_out:
            lines.append(f"      已排除: {'; '.join(item.ruled_out)}")
    return "\n".join(lines) if picked else ""


def _summary_text(confirmed: Mapping[str, Any]) -> str:
    """结构化结论 -> 一行「键=值」要点(非文本取值转 JSON);超长截断:全文不属于提示词。"""
    pairs = [(key, item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
             for key, item in confirmed.items()]
    text = "; ".join(f"{key}={item}" for key, item in pairs)
    return text if len(text) <= ANNOTATION_CHARS else text[:ANNOTATION_CHARS] + "…"
