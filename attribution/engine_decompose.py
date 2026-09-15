"""LMDI 分解的编排层(REUSE_DESIGN.md §4.2):AttributionEngine.decompose 的实现主体。

放 engine.py 之外只为单文件 ≤300 行。纯数值在 attribution.decompose,本模块只做
取数计划、调 engine 取数、调纯函数并整形;不认识具体数据集也不认识 DuckDB,受 AST
扫描约束:可执行代码不得出现数据集词汇(docstring/注释除外)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from attribution.decompose import (
    KIND_ADDITIVE, KIND_MULTIPLICATIVE, KIND_RATIO, KIND_STRUCTURAL,
    DecomposeError, decompose_additive, decompose_multiplicative,
    decompose_ratio, decompose_structural,
)

if TYPE_CHECKING:   # 仅类型标注用,避免运行时环:engine -> 本模块 -> engine
    from attribution.engine import AttributionEngine

__all__ = ["decompose_for"]


def decompose_for(
    engine: "AttributionEngine",
    target: str,
    factors: list[str],
    base_start: str,
    base_end: str,
    cmp_start: str,
    cmp_end: str,
    dimension: str | None = None,
    level: str | None = None,
    filters: dict[str, Any] | None = None,
    top_k: int = 5,
) -> dict:
    """分解编排:按语义层声明取数、调 attribution.decompose、整形为契约 dict。

    完整契约(逐键含义、边界、错误)见 AttributionEngine.decompose 的 docstring——
    那是事实来源,本函数只是实现主体。要点:声明命中即得 kind,未命中抛 SemanticError;
    structural 额外取实体级数据(下架/新上实体单列 entity_changes,不并入 effects);
    带 dimension/level 时逐切片分解、按 |total_change| 取前 top_k;切片单侧缺失按 0;
    factor 的 label 走语义层,数值走 engine 规范化。
    """
    decl = engine.semantic.decomposition_for(target, factors)   # 未声明 -> SemanticError
    filters = dict(filters or {})
    base_window, cmp_window = (base_start, base_end), (cmp_start, cmp_end)

    entity_changes = None
    if decl.kind == KIND_STRUCTURAL:
        totals, effects, entity_changes = _structural_window(
            engine, decl, target, filters, base_window, cmp_window)
    else:
        values = _window_values(engine, target, [target, *decl.factors], filters,
                                base_window, cmp_window)
        totals = (values[target][0], values[target][1])
        effects = [_effect(item) for item in _dispatch(engine, decl, target, values)]
    result = {
        "target": target,
        "kind": decl.kind,
        "factors": list(decl.factors),
        "dimension": dimension,
        "level": level,
        "total_base": _rounded(totals[0]),
        "total_cmp": _rounded(totals[1]),
        "total_change": _rounded(totals[1] - totals[0]),
        "effects": effects,
    }
    if entity_changes is not None:
        result["entity_changes"] = entity_changes
    if dimension is not None or level is not None:
        result["slices"] = _slices(engine, decl, target, filters, base_window, cmp_window,
                                   dimension, level, top_k)
    return result


def _window_values(engine, target, names, filters, base_window, cmp_window):
    """整窗取值 -> {指标名: [基期值, 对比期值]};缺失按 0,target 两侧无数据如实报错(§5.4)。"""
    values = {name: [engine._scalar(name, filters, *base_window),
                     engine._scalar(name, filters, *cmp_window)] for name in names}
    if any(value is None for value in values[target]):
        raise DecomposeError(
            f"指标 {target} 在其中一个窗口内没有任何数据:没有行不等于 0,无法分解")
    return {name: [_number(pair[0]), _number(pair[1])] for name, pair in values.items()}


def _dispatch(engine, decl, target, values):
    """按声明的 kind 分派到纯函数(取值口径一致的三类;structural 另走实体级取数)。"""
    if decl.kind == KIND_RATIO:
        numerator, denominator = (values[name] for name in decl.factors)
        # 展示名走模块缺省(分子效应 / 分母效应):入参是原始比率,不套用指标的 label
        return decompose_ratio(numerator[0], numerator[1], denominator[0], denominator[1])
    if decl.kind in (KIND_MULTIPLICATIVE, KIND_ADDITIVE):
        labels = {name: engine.semantic.metric(name).label for name in decl.factors}
        build = (decompose_multiplicative if decl.kind == KIND_MULTIPLICATIVE
                 else decompose_additive)
        return build(values[target][0], values[target][1],
                     {name: values[name][0] for name in decl.factors},
                     {name: values[name][1] for name in decl.factors}, labels=labels)
    raise DecomposeError(f"未知的分解类型 {decl.kind!r}:语义层校验本应拦住它")


def _ratio_members(engine, target) -> tuple[str, str]:
    """结构分解泛化规则:target 必须是「分子/分母」形(depends_on 恰两个,顺序 [分子,分母]),不认具体指标名;依赖数不对抛 ValueError。"""
    depends = engine.semantic.metric(target).depends_on
    if len(depends) != 2:
        raise ValueError(f"结构分解要求指标 {target} 是「分子 / 分母」形(依赖恰两个),"
                         f"实际依赖 {list(depends)}")
    return depends[0], depends[1]


def _entity_cells(engine, decl, target, filters, base_window, cmp_window, group_fields):
    """实体级两窗口取值 -> {分组键: {实体键: [n_0, n_t, d_0, d_t]}};缺失单元格 0。
    分组字段是 [层级字段, 实体键](切片)或 [实体键](整窗,分组键恒 ())。"""
    numerator, denominator = _ratio_members(engine, target)
    entity = engine.semantic.dimension(decl.entity_dimension).key
    fields = [*group_fields, entity]
    cells: dict = {}
    for name, slot, window in ((numerator, 0, base_window), (numerator, 1, cmp_window),
                               (denominator, 2, base_window), (denominator, 3, cmp_window)):
        for row in engine.source.aggregate(name, fields, filters, *window):
            group = tuple(_plain(row[field]) for field in fields[:-1])
            cell = cells.setdefault(group, {}).setdefault(
                _plain(row[entity]), [0.0, 0.0, 0.0, 0.0])
            cell[slot] = _number(row["v"])
    return cells


def _structural_window(engine, decl, target, filters, base_window, cmp_window):
    """整窗结构分解:实体级取数 -> 归一化 -> 结构/自身两效应 + 下架/新上实体条目。"""
    cells = _entity_cells(engine, decl, target, filters, base_window, cmp_window, [])
    group_cells = cells.get(())
    split = _structural_split(group_cells)
    if split is None:
        raise DecomposeError(
            f"指标 {target} 在实体维度 {decl.entity_dimension} 上没有可用切片"
            "(分母为 0),无法做结构分解")
    total_base, total_cmp, weights_base, weights_cmp, rates_base, rates_cmp = split
    effects = decompose_structural(total_base, total_cmp, weights_base, weights_cmp,
                                   rates_base, rates_cmp)
    labels = engine.source.entity_labels(decl.entity_dimension)
    changes = _structural_entity_changes(group_cells, labels)
    return (total_base, total_cmp), [_effect(item) for item in effects], changes


def _structural_entity_changes(cells, labels):
    """下架/新上实体(任一期分母为 0)单列:它们是结构变化的信号,是归因要找的根因。"""
    entries = []
    for entity in sorted((cells or {}), key=str):
        cell = cells[entity]
        if cell[2] > 0 and cell[3] > 0:
            continue    # 两侧分母都为正的实体进入 LMDI,不在这里重复列
        entries.append({
            "entity": _plain(entity),
            "label": _plain(labels.get(entity)) if labels.get(entity) is not None else _plain(entity),
            "only_in": "base" if cell[2] > 0 else "cmp",
            "numerator_base": _rounded(cell[0]),
            "numerator_cmp": _rounded(cell[1]),
            "denominator_base": _rounded(cell[2]),
            "denominator_cmp": _rounded(cell[3]),
        })
    return entries


def _structural_split(cells):
    """分组内实体单元格 -> (总量, 权重×2, 强度×2);无可用的返回 None。
    分母为 0 的实体两侧丢弃(权重与强度须配对),由 _structural_entity_changes 单列;
    总量取保留实体的 Σ分子/Σ分母,与 Σ w·r 同源,正是定义式。"""
    kept = {entity: cell for entity, cell in (cells or {}).items()
            if cell[2] > 0 and cell[3] > 0}
    if not kept:
        return None
    sum_base, sum_cmp = (sum(cell[slot] for cell in kept.values()) for slot in (2, 3))
    weights_base = {entity: cell[2] / sum_base for entity, cell in kept.items()}
    weights_cmp = {entity: cell[3] / sum_cmp for entity, cell in kept.items()}
    rates_base = {entity: cell[0] / cell[2] for entity, cell in kept.items()}
    rates_cmp = {entity: cell[1] / cell[3] for entity, cell in kept.items()}
    return (sum(cell[0] for cell in kept.values()) / sum_base,
            sum(cell[1] for cell in kept.values()) / sum_cmp,
            weights_base, weights_cmp, rates_base, rates_cmp)


def _slices(engine, decl, target, filters, base_window, cmp_window, dimension, level, top_k):
    """切片分解结果,已排序截断。level 必须是该维度的层级字段(与 contribute 同口径)。"""
    if level not in engine.semantic.dimension(dimension).hierarchy:
        raise ValueError(f"维度 {dimension} 无层级字段 {level}")
    if decl.kind == KIND_STRUCTURAL:
        frames = _structural_slices(engine, decl, target, filters, base_window, cmp_window,
                                    dimension, level)
    else:
        frames = _metric_slices(engine, decl, target, filters, base_window, cmp_window,
                                dimension, level)
    # 先按 key 定序再按 |变化量| 排序:并列时输出稳定(同输入 -> 同输出,评估基准不被行序污染)
    frames.sort(key=lambda frame: str(frame["key"]))
    frames.sort(key=lambda frame: -abs(frame["total_change"] or 0.0))
    return frames[:top_k]


def _metric_slices(engine, decl, target, filters, base_window, cmp_window, dimension, level):
    """指标因子类(multiplicative / additive / ratio)的切片形态,逐切片调纯函数。"""
    names = [target, *decl.factors]
    tables = {name: _slice_map(engine.source.slice_rows(name, dimension, level,
                                                        base_window, cmp_window))
              for name in names}
    keys: dict = {}
    for table in tables.values():      # 各指标切片键的并集:只在单侧出现的切片同样参与
        keys.update(dict.fromkeys(table))
    frames = []
    for key in sorted(keys, key=str):
        values = {name: list(tables[name].get(key, (None, 0.0, 0.0))[1:]) for name in names}
        if decl.kind == KIND_RATIO:
            denominator = values[decl.factors[-1]]
            if not denominator[0] or not denominator[1]:
                continue    # 分母为 0 的切片:比率无定义,直接跳过(不参与也不记)
        label = next((tables[name][key][0] for name in names if key in tables[name]), key)
        frames.append(_frame(key, label, values[target],
                             _dispatch(engine, decl, target, values)))
    return frames


def _structural_slices(engine, decl, target, filters, base_window, cmp_window,
                       dimension, level):
    """结构分解切片形态:实体先按 (层级字段, 实体键) 分组,再逐切片归一化分解;每片同样带 entity_changes。"""
    cells = _entity_cells(engine, decl, target, filters, base_window, cmp_window, [level])
    # 展示名与 key 口径与 contribute 一致:key 层用 name_column(由 slice_rows 给出)
    labelled = _slice_map(engine.source.slice_rows(target, dimension, level,
                                                   base_window, cmp_window))
    labels = engine.source.entity_labels(decl.entity_dimension)
    frames = []
    for group, group_cells in cells.items():
        split = _structural_split(group_cells)
        if split is None:
            continue
        effects = decompose_structural(*split)
        entry = labelled.get(group[0]) if group else None
        frame = _frame(group[0], entry[0] if entry else group[0],
                       (split[0], split[1]), effects)
        frame["entity_changes"] = _structural_entity_changes(group_cells, labels)
        frames.append(frame)
    return frames


def _slice_map(rows):
    """slice_rows 的 [(label, key, base, cmp)] -> {key: (label, base, cmp)}。

    缺失一侧(None)按 0 处理;同一 key 出现多行时累加,与 contribute 的合并口径一致。
    """
    merged: dict = {}
    for label, key, base, cmp_value in rows:
        seen = merged.get(key)
        merged[key] = (label, _number(base), _number(cmp_value)) if seen is None else (
            seen[0], seen[1] + _number(base), seen[2] + _number(cmp_value))
    return merged


def _frame(key, label, totals, effects) -> dict:
    """一个切片的结果条目:key / label 用原始取值,数值经规范化。"""
    return {
        "key": _plain(key),
        "label": _plain(label),
        "total_base": _rounded(totals[0]),
        "total_cmp": _rounded(totals[1]),
        "total_change": _rounded(totals[1] - totals[0]),
        "effects": [_effect(item) for item in effects],
    }


def _effect(effect) -> dict:
    """Effect(dataclass)-> 契约条目;change_rate/contribution 已由纯函数算好,不重算。"""
    return {
        "factor": effect.factor,
        "label": effect.label,
        "base": _rounded(effect.base),
        "cmp": _rounded(effect.cmp),
        "effect": _rounded(effect.effect),
        "contribution": _rounded(effect.contribution),
        "change_rate": _rounded(effect.change_rate),
    }


def _number(value) -> float:
    """缺失(窗口内无数据)-> 0.0:纯函数只接受数值,而「没有行」在切片里按 0 参与。"""
    return 0.0 if value is None else float(value)


# 数值规范化与类型归一复用 engine 的实现;延迟到调用期导入 engine,避免导入期成环。
def _rounded(value):
    """engine._rnd 的口径:None / NaN -> None,其余转 float。"""
    from attribution.engine import _rnd
    return _rnd(value, nd=None)


def _plain(value):
    """engine._native:把 numpy 标量转成 Python 原生类型(保证 JSON 可序列化)。"""
    from attribution.engine import _native
    return _native(value)
