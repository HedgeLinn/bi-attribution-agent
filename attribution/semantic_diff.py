"""语义层 diff:对比两版地图,逐条判定「增量」还是「破坏性」(docs/REUSE_DESIGN.md §3.6②③)。

为什么必须机械检测:语义层是下钻归因的地图,改地图分两类,代价差一个数量级——
加指标 / 加维度 / 改展示名只影响展示,而改 expression(口径)、删指标或维度、
改 hierarchy 顺序、改 time_aggregation 会让**历史数值不可比**、让 case 的期望值失效。
后者必须 bump dataset_version 并重算受影响 case(§3.6①)。人记不住这些:一次
「顺手把按时间求和改成取期末值」的改动在 diff 里只占一行,却让所有历史结论静默失真
(§1.3 的静默错误不报错,只给一个错的数)。所以级别判定写死在代码里(见
attribution.semantic_diff_rules 的级别表),由 `scripts/check_semantic.py` 在评审 /
CI 里执行,而不是靠人记。

级别判定的两条口径:

    - **破坏性**:历史数值不再可比,或既有 case 的期望值与下钻路径失效
    - **增量**:只新增可用对象,或只改展示与知识;历史数值与既有期望值仍成立

刻意选定的边界判定(有争议,故写明理由):

    - ``hierarchy`` **末尾追加**一层 = 增量:既有层级的顺序与序号都没变,老 case 的
      下钻路径与 required_depth 仍成立(与 §3.6② 表里「加层级 = 增量」一致);
      而**插入 / 前置 / 重排 / 删除**都是破坏性——既有层级的序号平移,路径变了
    - ``depends_on`` **按集合比**:编译与校验都把它当无序依赖集,纯换序不产生任何
      变更条目(免得无意义的换序被报成破坏性,让人开始忽略这个工具);成员增删是
      破坏性——表达式引用随之改变,口径变了
    - ``time_aggregation`` 的 sum -> last 是最严重的一类,detail 直接点名 §1.3
    - ``schema_version`` / ``dataset`` / ``fact_table`` / ``date_field`` 变化 = 破坏性:
      §3.6② 的表没列它们,但换结构规范 / 换地图 / 换事实表 / 换时间轴都会让历史评估
      结果整体失去可比性——宁可误报,不可漏报
    - ``caveats`` 与 ``time.calendar`` 是**知识**不是口径:增删改都不改变任何数值,
      一律增量(删掉日历知识会让未来的结论变差,但那不是「历史数值失效」)
    - ``decompositions`` 改已有 target 的声明 = 破坏性(case 的期望分解结构失效),
      新增一个 target = 增量

输入接受 `str` / `Path`(语义层 YAML 路径,最常用)与已加载的 `Semantic` 实例。
**`Semantic` 实例不保留 YAML 原文**,`decompositions` / `time.calendar` / `caveats`
三段无从对比(该来源的这三段标记为「不可比」而非「空」,不会误报成删除);
要完整对比请传文件路径。两种来源的实体部分走同一批 dataclass 字段,结果一致。

输出按级别排序(破坏性在前),同级内按 path 排序——同一对输入的输出稳定可比对。
"""

from collections.abc import Mapping, Sequence
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

from attribution.semantic import Dimension, Metric, Semantic, SemanticError, load_yaml
from attribution.semantic_diff_rules import (  # 判定规则与级别表住 rules,这里只做归一化与编排
    SECTION_CALENDAR, SECTION_CAVEATS, SECTION_DECOMPOSITIONS, SECTION_DIMENSIONS,
    SECTION_METRICS, SECTION_TIME, Change, KIND_ADDED, KIND_MODIFIED, KIND_REMOVED,
    LEVEL_BREAKING, LEVEL_INCREMENTAL, diff_entity_section, diff_header_fields,
    render_value, sort_changes,
)

__all__ = ["Change", "LEVEL_BREAKING", "LEVEL_INCREMENTAL", "diff_layers", "has_breaking"]

# detail 里最多列几条条目,再多就折叠成计数(说明要能一眼读完)
_MAX_ITEMS_IN_DETAIL = 3

# 分解声明条目的主键 / 实体身份字段:两者都用来给条目编 path,不参与取值比较
_TARGET_FIELD = "target"
_IDENTITY_FIELD = "name"


# ----------------------------------------------------------------------
# 公共入口
# ----------------------------------------------------------------------
def diff_layers(old: str | Path | Semantic, new: str | Path | Semantic) -> list[Change]:
    """对比两版语义层,逐条判定变更级别;返回按级别(path)排序的变更列表。

    old / new:语义层 YAML 路径(str / Path)或已加载的 Semantic 实例。
    无变更时返回空列表;语义层结构非法(加载失败)抛 SemanticError;
    来源类型不支持抛 TypeError。
    """
    old_document, new_document = _normalized(old), _normalized(new)
    changes = diff_header_fields(old_document, new_document)
    changes += diff_entity_section(SECTION_METRICS, old_document, new_document)
    changes += diff_entity_section(SECTION_DIMENSIONS, old_document, new_document)
    changes += _knowledge_changes(old_document, new_document)
    return sort_changes(changes)


def has_breaking(changes: Sequence[Change]) -> bool:
    """是否含破坏性变更;调用方据此决定退出码、以及要不要 bump dataset_version。"""
    return any(change.level == LEVEL_BREAKING for change in changes)


# ----------------------------------------------------------------------
# 来源归一化:两种输入形态 -> 同一份可比对的文档
# ----------------------------------------------------------------------
def _normalized(source: str | Path | Semantic) -> dict[str, Any]:
    """语义层来源 -> 归一化文档:头部标量 + 实体字段字典 + 三段未实体化的原文。

    实体部分两种来源都走同一批 dataclass 字段(parse_layer 的产物),结果一致;
    只有 YAML 原文里才有的三段(decompositions / time.calendar / caveats)在
    Semantic 实例来源下标记为 None(不可比,区别于「两边都空」)。
    """
    if isinstance(source, Semantic):
        return {
            **_header_of(source),
            SECTION_METRICS: {n: _metric_fields(m) for n, m in source.metrics.items()},
            SECTION_DIMENSIONS: {n: _entity_fields(d) for n, d in source.dimensions.items()},
            SECTION_DECOMPOSITIONS: None,
            SECTION_CALENDAR: None,
            SECTION_CAVEATS: None,
        }
    if isinstance(source, (str, Path)):
        path = str(source)
        document = _normalized(Semantic.load(path))
        raw = load_yaml(path)   # 未被实体化的三段只存在于 YAML 原文
        document.update(_raw_sections(raw if isinstance(raw, Mapping) else {}))
        return document
    raise TypeError(f"语义层来源只支持文件路径或 Semantic 实例,得到 {type(source).__name__}")


def _header_of(semantic: Semantic) -> dict[str, Any]:
    """头部标量字段;取值一律走公共属性,不碰私有状态。"""
    return {
        "schema_version": semantic.schema_version,
        "dataset": semantic.dataset,
        "dataset_version": _safe_dataset_version(semantic),
        "fact_table": semantic.fact_table,
        "date_field": semantic.date_field,
    }


def _safe_dataset_version(semantic: Semantic) -> str:
    """取 dataset_version;缺失时给空串——缺字段由 §3.5 校验负责报,diff 不重复报。"""
    try:
        return semantic.dataset_version
    except SemanticError:
        return ""


def _entity_fields(entity: Metric | Dimension) -> dict[str, Any]:
    """dataclass 实体 -> 字段字典。实体名是身份(用在 path 里),不参与取值比较。"""
    return {spec.name: getattr(entity, spec.name)
            for spec in dataclass_fields(entity) if spec.name != _IDENTITY_FIELD}


def _metric_fields(metric: Metric) -> dict[str, Any]:
    """指标字段字典;依赖抹成集合——它是无序依赖集,纯换序不算变更(见模块 docstring)。"""
    return {**_entity_fields(metric), "depends_on": frozenset(metric.depends_on)}


def _raw_sections(raw: Mapping) -> dict[str, Any]:
    """从 YAML 原文取出三段未被实体化的语义:分解声明 / 促销日历 / 口径陷阱。

    它们的结构由数据集自由决定(引擎不解析),这里只做「按主键索引」的归一化,
    以便逐条对比、并在 detail 里指出改的是哪一条。
    """
    decompositions = {_entry_key(entry, index): entry
                      for index, entry in enumerate(_as_list(raw.get(SECTION_DECOMPOSITIONS)))}
    calendar = {str(name): entries for name, entries in _as_mapping(
        _as_mapping(raw.get(SECTION_TIME)).get(SECTION_CALENDAR)).items()}
    return {
        SECTION_DECOMPOSITIONS: decompositions,
        SECTION_CALENDAR: calendar,
        SECTION_CAVEATS: _as_list(raw.get(SECTION_CAVEATS)),
    }


# ----------------------------------------------------------------------
# 三段「知识」:分解声明 / 促销日历 / 口径陷阱
# ----------------------------------------------------------------------
def _knowledge_changes(old_document: Mapping, new_document: Mapping) -> list[Change]:
    """三段知识块的对比;其中一段任一侧不可比(Semantic 实例来源)则跳过该段。"""
    changes: list[Change] = []
    for section, compare in ((SECTION_DECOMPOSITIONS, _decomposition_changes),
                             (SECTION_CALENDAR, _calendar_changes),
                             (SECTION_CAVEATS, _caveats_changes)):
        old_value, new_value = old_document.get(section), new_document.get(section)
        if old_value is not None and new_value is not None:
            changes += compare(old_value, new_value)
    return changes


def _decomposition_changes(old_entries: Mapping, new_entries: Mapping) -> list[Change]:
    """分解声明:新增 target = 增量;删除 / 修改已有 target = 破坏性(期望分解结构失效)。"""
    changes: list[Change] = []
    for key in sorted(set(old_entries) | set(new_entries)):
        old_entry, new_entry = old_entries.get(key), new_entries.get(key)
        path = f"{SECTION_DECOMPOSITIONS}.{key}"
        if old_entry is None:
            changes.append(Change(KIND_ADDED, LEVEL_INCREMENTAL, path,
                                  f"新增分解声明 {key}:{render_value(new_entry)};老 case 不受影响"))
        elif new_entry is None:
            changes.append(Change(KIND_REMOVED, LEVEL_BREAKING, path,
                                  f"删除分解声明 {key}:依赖该分解的 case 期望结构失效"))
        elif old_entry != new_entry:
            changes.append(Change(KIND_MODIFIED, LEVEL_BREAKING, path,
                                  f"分解声明 {key} 变化:{render_value(old_entry)} -> "
                                  f"{render_value(new_entry)};因子或分解方式变了,期望结构失效"))
    return changes


def _calendar_changes(old_entries: Mapping, new_entries: Mapping) -> list[Change]:
    """促销日历:知识块,增删改一律增量——它不改变任何 expression 的取值。"""
    changes: list[Change] = []
    for name in sorted(set(old_entries) | set(new_entries)):
        old_value, new_value = old_entries.get(name), new_entries.get(name)
        if old_value == new_value:
            continue
        if old_value is None:
            kind = KIND_ADDED
        elif new_value is None:
            kind = KIND_REMOVED
        else:
            kind = KIND_MODIFIED
        changes.append(Change(
            kind, LEVEL_INCREMENTAL, f"{SECTION_TIME}.{SECTION_CALENDAR}.{name}",
            f"促销日历 {name}:{render_value(old_value)} -> {render_value(new_value)};"
            f"仅知识展示,不影响历史数值"))
    return changes


def _caveats_changes(old_value: Any, new_value: Any) -> list[Change]:
    """口径陷阱:知识块,增删改一律增量(§3.6② 明确「加 caveats = 增量」)。"""
    old_items, new_items = list(_as_list(old_value)), list(_as_list(new_value))
    if old_items == new_items:
        return []
    added = [item for item in new_items if item not in old_items]
    removed = [item for item in old_items if item not in new_items]
    parts = []
    if added:
        parts.append(f"新增 {_describe_items(added)}")
    if removed:
        parts.append(f"删除 {_describe_items(removed)}")
    if not parts:   # 条目没变,只是顺序调整
        parts.append("顺序调整")
    return [Change(KIND_MODIFIED, LEVEL_INCREMENTAL, SECTION_CAVEATS,
                   f"口径陷阱{'、'.join(parts)};仅知识展示,不影响历史数值")]


# ----------------------------------------------------------------------
# 内部:小工具
# ----------------------------------------------------------------------
def _entry_key(entry: Any, index: int) -> str:
    """分解条目的主键:优先用 target;没有就退回序号——非法条目也要让 diff 看得见。"""
    if isinstance(entry, Mapping) and entry.get(_TARGET_FIELD) is not None:
        return str(entry[_TARGET_FIELD])
    return f"#{index}"


def _describe_items(items: Sequence[Any]) -> str:
    """条目列表渲染成一行;超过上限折叠成计数。"""
    shown = "、".join(render_value(item) for item in items[:_MAX_ITEMS_IN_DETAIL])
    hidden = len(items) - _MAX_ITEMS_IN_DETAIL
    return f"{shown} 等 {len(items)} 条" if hidden > 0 else shown


def _as_list(value: Any) -> list[Any]:
    """把 标量 / 列表 / None 归一成列表;非法形态包成单元素,不静默丢弃。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _as_mapping(value: Any) -> Mapping:
    """取映射;非映射(None / 标量)退化成空映射。"""
    return value if isinstance(value, Mapping) else {}
