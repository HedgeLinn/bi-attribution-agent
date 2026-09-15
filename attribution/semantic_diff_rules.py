"""语义层 diff 的**判定规则**:级别表 + 逐字段判定 + 说明渲染。

契约与边界判定的完整理由见 `attribution.semantic_diff` 的模块文档;本模块只承载
「拿到两版地图的字段值,判它是增量还是破坏性」这件事,全是纯函数:

    - 入参只有归一化后的字段值(Mapping / 标量),出参是 `Change` 列表
    - **不做任何 IO**:不读 YAML、不解析语义层、不认识 Semantic / 引擎
    - 不认识具体数据集:级别表按字段名组织,与数据集内容无关

级别表就是判定依据,所以与代码放在一起而不是写进文档:文档会过期,表不会。
`_assert_levels_registered` 在导入期自检「实体的每个字段都登记了级别」——
新字段漏登记会让 diff 悄悄漏检,那是本模块最不可接受的失败方式,故让它直接炸。
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields as dataclass_fields
from typing import Any

from attribution.semantic import AGG_LAST, AGG_SUM, Dimension, Metric

__all__ = [
    "Change", "KIND_ADDED", "KIND_MODIFIED", "KIND_REMOVED", "LEVEL_BREAKING",
    "LEVEL_INCREMENTAL", "LEVEL_ORDER", "SECTION_CALENDAR", "SECTION_CAVEATS",
    "SECTION_DECOMPOSITIONS", "SECTION_DIMENSIONS", "SECTION_METRICS", "SECTION_TIME",
    "diff_entity_section", "diff_header_fields", "render_value", "sort_changes",
]

# 变更形态(Change.kind 的取值)
KIND_ADDED = "added"
KIND_REMOVED = "removed"
KIND_MODIFIED = "modified"

# 变更级别(Change.level 的取值)
LEVEL_INCREMENTAL = "incremental"
LEVEL_BREAKING = "breaking"

# 输出排序权重:破坏性在前,同级内按 path(输出稳定才可比对)
LEVEL_ORDER: Mapping[str, int] = {LEVEL_BREAKING: 0, LEVEL_INCREMENTAL: 1}

# 归一化文档的段落名;后三个同时也是 YAML 原文里的键,故兼作取值键
SECTION_METRICS = "metrics"
SECTION_DIMENSIONS = "dimensions"
SECTION_DECOMPOSITIONS = "decompositions"
SECTION_TIME = "time"
SECTION_CALENDAR = "calendar"
SECTION_CAVEATS = "caveats"

# 段落键集合:归一化文档里除它们之外的顶层键都是头部标量字段
_SECTION_KEYS = frozenset({
    SECTION_METRICS, SECTION_DIMENSIONS, SECTION_DECOMPOSITIONS,
    SECTION_CALENDAR, SECTION_CAVEATS,
})

# 段落 -> 中文名(说明里点明改的是指标还是维度);实体名是身份,不参与取值比较
_SECTION_WORDS: Mapping[str, str] = {SECTION_METRICS: "指标", SECTION_DIMENSIONS: "维度"}
_IDENTITY_FIELD = "name"

# 头部标量字段 -> (变更级别, 中文说明)
_HEADER_RULES: Mapping[str, tuple[str, str]] = {
    "schema_version": (
        LEVEL_BREAKING, "结构规范版本变化:字段语义可能被重定义,diff 只看得见字段值、看不见语义"),
    "dataset": (
        LEVEL_BREAKING, "数据集标识变化:这已经是另一张地图,历史评估结果整体不可比"),
    "dataset_version": (
        LEVEL_INCREMENTAL, "地图实例版本更新(元信息变化,本身不改变任何数值)"),
    "fact_table": (
        LEVEL_BREAKING, "换了事实表:所有指标的取数来源变了,历史数值不可比"),
    "date_field": (
        LEVEL_BREAKING, "换了时间轴:所有时间切片与趋势不可比"),
}

# 指标字段 -> 变更级别(§3.6②:口径 / 类型 / 聚合语义 / 依赖变化都是破坏性)
_METRIC_LEVELS: Mapping[str, str] = {
    "expression": LEVEL_BREAKING,
    "type": LEVEL_BREAKING,
    "time_aggregation": LEVEL_BREAKING,
    "depends_on": LEVEL_BREAKING,
    "source": LEVEL_BREAKING,
    "label": LEVEL_INCREMENTAL,
    "unit": LEVEL_INCREMENTAL,
}

# 维度字段 -> 变更级别;hierarchy 是特例(末尾追加降为增量),交给 _hierarchy_change
_DIMENSION_LEVELS: Mapping[str, str] = {
    "table": LEVEL_BREAKING,
    "key": LEVEL_BREAKING,
    "kind": LEVEL_BREAKING,
    "hierarchy": LEVEL_BREAKING,
    "name_column": LEVEL_INCREMENTAL,
    "label": LEVEL_INCREMENTAL,
    "drill_priority": LEVEL_INCREMENTAL,
}

# path 用 YAML 里的键名(读者能直接拿着它去地图里找)。只有一处需要改名:
# 维度类型在 YAML 里写作 type,而解析后的 dataclass 字段名是 kind
_PATH_KEYS: Mapping[str, str] = {"kind": "type"}


@dataclass(frozen=True)
class Change:
    """一条变更。path 是定位式路径(如 metrics.aov.expression),detail 是人类可读的中文说明。"""

    kind: str        # added / removed / modified
    level: str       # incremental / breaking
    path: str        # 定位式路径,如 metrics.aov.expression
    detail: str      # 人类可读说明(中文)


def sort_changes(changes: Sequence[Change]) -> list[Change]:
    """排序:破坏性在前,同级内按 path;并列时再按 kind / detail,保证输出完全确定。"""
    return sorted(changes, key=lambda change: (
        LEVEL_ORDER.get(change.level, 0), change.path, change.kind, change.detail))


# ----------------------------------------------------------------------
# 头部标量字段
# ----------------------------------------------------------------------
def diff_header_fields(old_document: Mapping, new_document: Mapping) -> list[Change]:
    """头部标量字段逐个比。级别取自 _HEADER_RULES;未登记字段按破坏性处理(漏登记=漏检)。"""
    changes: list[Change] = []
    for field in sorted(set(old_document) | set(new_document)):
        if field in _SECTION_KEYS:
            continue
        old_value, new_value = old_document.get(field), new_document.get(field)
        if old_value == new_value:
            continue
        level, note = _HEADER_RULES.get(field, (LEVEL_BREAKING, "未登记的头字段:按破坏性处理"))
        changes.append(Change(KIND_MODIFIED, level, field,
                              f"{field}: {old_value} -> {new_value} —— {note}"))
    return changes


# ----------------------------------------------------------------------
# 指标段 / 维度段
# ----------------------------------------------------------------------
def diff_entity_section(section: str, old_document: Mapping,
                        new_document: Mapping) -> list[Change]:
    """指标段 / 维度段的整体对比:整条新增、整条删除、逐字段修改。

    级别表与特例判级函数都按段落名在本模块内部取,调用方只报「比哪一段」,
    免得两处各拿一份表却拿错了对象。
    """
    levels, special = _SECTION_RULES[section]
    old_entries = old_document.get(section) or {}
    new_entries = new_document.get(section) or {}
    changes: list[Change] = []
    for name in sorted(set(old_entries) | set(new_entries)):
        old_fields, new_fields = old_entries.get(name), new_entries.get(name)
        if old_fields is None:
            changes.append(_entity_change(KIND_ADDED, section, name, new_fields))
        elif new_fields is None:
            changes.append(_entity_change(KIND_REMOVED, section, name, old_fields))
        else:
            changes += _field_changes(section, name, old_fields, new_fields, levels, special)
    return changes


def _entity_change(kind: str, section: str, name: str, fields: Mapping[str, Any]) -> Change:
    """整条实体新增 / 删除。新增是增量;删除是破坏性——引用它的 case 直接失效。"""
    added = kind == KIND_ADDED
    level = LEVEL_INCREMENTAL if added else LEVEL_BREAKING
    verb = "新增" if added else "删除"
    tail = "老 case 不受影响" if added else "引用它的 case 直接失效"
    what = f"{_SECTION_WORDS.get(section, section)} {name}({_render_entity(section, fields)})"
    return Change(kind, level, f"{section}.{name}", f"{verb}{what}:{tail}")


def _field_changes(section: str, name: str, old_fields: Mapping[str, Any],
                   new_fields: Mapping[str, Any], levels: Mapping[str, str],
                   special: Mapping[str, Callable[[str, Any, Any], Change]]) -> list[Change]:
    """一个实体的逐字段对比;special 里的字段交给专门函数判级。"""
    changes: list[Change] = []
    for field, level in levels.items():
        old_value, new_value = old_fields.get(field), new_fields.get(field)
        if old_value == new_value:
            continue
        path = f"{section}.{name}.{_PATH_KEYS.get(field, field)}"
        judge = special.get(field)
        if judge is not None:
            changes.append(judge(path, old_value, new_value))
        else:
            changes.append(Change(KIND_MODIFIED, level, path,
                                  _field_detail(name, field, old_value, new_value, level)))
    return changes


def _field_detail(name: str, field: str, old_value: Any, new_value: Any, level: str) -> str:
    """字段变更的中文说明:口径 / 聚合语义 / 依赖这三类要写清后果,其余给出前后取值。"""
    if field == "expression":
        return (f"指标 {name} 的口径变化:{old_value} -> {new_value};"
                f"历史数值不可比,相关 case 的贡献区间全部失效")
    if field == "time_aggregation":
        return (f"指标 {name} 的时间聚合语义 {old_value} -> {new_value}"
                f"{_aggregation_risk(old_value, new_value)}")
    if field == "depends_on":   # 只有成员变化会走到这里(换序已在归一化时抹平)
        return (f"指标 {name} 的依赖集合变化:{_render_names(old_value)} -> "
                f"{_render_names(new_value)};表达式引用随之改变,口径变了")
    if field == "label":
        return f"{name} 的展示名 {old_value} -> {new_value};仅展示变化,不影响历史数值"
    tail = "仅展示或元信息变化" if level == LEVEL_INCREMENTAL else "历史数值或下钻路径随之变化"
    return f"{name} 的 {field} 变化:{render_value(old_value)} -> {render_value(new_value)};{tail}"


def _aggregation_risk(old_value: Any, new_value: Any) -> str:
    """时间聚合变化的后果说明;sum -> last 是 §1.3 的静默错误,必须点名。"""
    if old_value == AGG_SUM and new_value == AGG_LAST:
        return (";从按时间求和改成取期末值——跨期数值会成倍虚高,而它不报错、只给"
                "一个错的数(§1.3 的静默错误),所有历史数值作废")
    if AGG_LAST in (old_value, new_value):
        return ";涉及期末值语义:半可加指标跨期不可求和,历史数值不可比"
    return ";所有历史数值随之变化,必须重算受影响 case"


def _hierarchy_change(path: str, old_value: Any, new_value: Any) -> Change:
    """下钻层级变化的判定:§3.6② 的「加层级=增量」与「改顺序=破坏性」在这里合流。

    末尾追加 = 增量:既有层级的顺序与序号都没变,老 case 的下钻路径与 required_depth
    仍成立;其余(插入 / 前置 / 重排 / 删除)都让既有层级的序号平移,路径变了。
    """
    old_levels, new_levels = tuple(old_value or ()), tuple(new_value or ())
    if old_levels == new_levels[:len(old_levels)]:   # 新层级以旧层级为前缀 -> 只追加
        return Change(KIND_ADDED, LEVEL_INCREMENTAL, path,
                      f"末尾追加下钻层级 {render_value(new_levels[len(old_levels):])};"
                      f"既有层级顺序与序号不变,老 case 的下钻路径仍成立")
    if new_levels == old_levels[:len(new_levels)]:   # 旧层级以新层级为前缀 -> 只删末尾
        return Change(KIND_REMOVED, LEVEL_BREAKING, path,
                      f"删除下钻层级 {render_value(old_levels[len(new_levels):])};"
                      f"沿这些层的切片与下钻直接失效")
    return Change(KIND_MODIFIED, LEVEL_BREAKING, path,
                  f"下钻层级顺序或组成变化:{render_value(old_levels)} -> "
                  f"{render_value(new_levels)};下钻路径变了,所有 required_depth 需复查")


# ----------------------------------------------------------------------
# 导入期自检与渲染工具
# ----------------------------------------------------------------------
def _assert_levels_registered(entity_type: type, levels: Mapping[str, str]) -> None:
    """实体的每个字段都必须在级别表里登记——漏登记等于悄悄漏检,故导入期就失败。"""
    unregistered = ({spec.name for spec in dataclass_fields(entity_type)}
                    - set(levels) - {_IDENTITY_FIELD})
    if unregistered:
        raise RuntimeError(
            f"{entity_type.__name__} 有字段未登记变更级别: {sorted(unregistered)}——"
            "新字段必须显式判定级别,否则 diff 会漏检")


_assert_levels_registered(Metric, _METRIC_LEVELS)
_assert_levels_registered(Dimension, _DIMENSION_LEVELS)

# 段落 -> (字段级别表, 特例字段判级函数)。hierarchy 光比较取值判不出级别,
# 末尾追加一层是增量而重排是破坏性,故走 _hierarchy_change
_SECTION_RULES: Mapping[str, tuple[Mapping[str, str], Mapping[str, Callable[..., Change]]]] = {
    SECTION_METRICS: (_METRIC_LEVELS, {}),
    SECTION_DIMENSIONS: (_DIMENSION_LEVELS, {"hierarchy": _hierarchy_change}),
}


def _render_entity(section: str, fields: Mapping[str, Any]) -> str:
    """实体的关键定义渲染成一行:指标给口径 + 类型/聚合,维度给下钻层级。"""
    if section == SECTION_METRICS:
        return (f"{fields.get('label')} = {fields.get('expression')}"
                f" [{fields.get('type')}/{fields.get('time_aggregation')}]")
    return f"{fields.get('label')},下钻 {' -> '.join(fields.get('hierarchy') or ())}"


def _render_names(values: Any) -> str:
    """无序集合(依赖集)渲染成排好序的名字,免得每次打印顺序都不一样。"""
    return "、".join(sorted(str(item) for item in values or ()))


def render_value(value: Any) -> str:
    """把字段值渲染成一行:结构化值走 JSON,标量直接转字符串。"""
    if value is None:
        return "(未声明)"
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)
