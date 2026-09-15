"""归因树 + 瀑布图:把 agent 的下钻过程与贡献度画成图(REUSE_DESIGN §4.6)。

两层结构:纯数据函数 + 渲染函数(只做展示;graphviz/altair 均 streamlit 内置,零新依赖)。
消费 harness/loop.py 的事件流({"type":"tool_call"/"tool_result", "step", "name",
"args", "result"},与 app/app.py 的 on_event 同构),只画 tool_result(已发生的事实)。
工具异常在 loop 里被包成 dict `{"error": str(e)}`——那是失败,不是「比率型不可加」:
必须如实标出(见 _push_node / render_from_events),不许替它编一个口径解释。

⚠️ 口径警告(契约 v2,画图时必须遵守):
    - structural 分解的 total_* 与 query_metric **不可比**(定义在保留实体上),
      不得把两者画进同一张瀑布图或同一条叙事线
    - structural 分解中分母为 0 的实体(如下架 SKU)不进 effects,而是单列进
      entity_changes(下架/新上根因实体看它,不看 effects)
"""
from __future__ import annotations

import math
from typing import Any

__all__ = ["build_tree", "extract_drilldown", "render_from_events", "render_tree",
           "render_waterfall", "waterfall_from_contribute", "waterfall_from_decompose"]

# 渲染常量:工具名(harness 对模型的稳定契约)/ 图标 / 配色 / 容差;**无任何数据集词汇**。
_ANOMALY, _CONTRIBUTE, _QUERY, _DECOMPOSE = (
    "detect_anomaly", "contribute", "query_metric", "decompose")
_STYLE = {_ANOMALY: ("🔍", "#FEF3C7"), _CONTRIBUTE: ("📊", "#EEF2FF"),
          _QUERY: ("🧮", "#F0FDF4"), _DECOMPOSE: ("🧩", "#F5F3FF")}
_FILL_DIM, _FILL_PLAIN = "#E0E7FF", "#F1F5F9"   # 维度节点(下钻入口)/ 未知或降级节点
_UP, _DOWN, _FLAT = "#16A34A", "#DC2626", "#94A3B8"
_TEXT, _DOT, _TOL = 60, 120, 1e-12              # 标签截断 / 残差容差(相对总量尺度)
_WARN = " ⚠️"                                   # 工具失败标记:报错绝不画成正常结果
_NO_SLICE, _UNREADABLE = "无切片明细(仅总量)", "结果不可读"

def _as_dict(value: Any) -> dict:
    """非 dict 一律退化成空 dict(事件与结果里什么都可能出现)。"""
    return value if isinstance(value, dict) else {}

def _num(value: Any) -> float | None:
    """数值化:bool / None / 字符串 / 非有限(nan/±inf)都算「没有数值」(不猜、不填 0)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None

def _text(value: Any, limit: int = _TEXT) -> str:
    """任意值 -> 单行短文本(None -> 空串);超长截断加省略号。"""
    text = " ".join(str(value).split()) if value is not None else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"

def _slice_label(entry: dict) -> str:
    """切片标签 = 切片名 + 变化量(additive 看 change,derived 退到 change_rate);
    数值必须有限(nan / ±inf 只留切片名),绝不产出假的 +nan / +inf 后缀。"""
    name = _text(entry.get("key")) or _text(entry.get("label"), 40) or "?"
    delta, rate = _num(entry.get("change")), _num(entry.get("change_rate"))
    delta, rate = [v if v is None or math.isfinite(v) else None for v in (delta, rate)]
    if delta is not None:
        return f"{name} {format(delta, '+,.0f')}"
    return f"{name} {rate:+.1%}" if rate is not None else name

def extract_drilldown(events: list[dict]) -> list[dict]:
    """事件流 -> 下钻链:按 step 升序取全部 tool_result(与它的 tool_call 配对)。
    result 是字符串(工具报错)时原样保留,由画图侧决定降级;未知工具名也保留。"""
    if not isinstance(events, list):
        return []
    pending: dict[tuple[str, str], list] = {}   # (step, name) -> 未消费的 args 队列
    items: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        key = (str(event.get("step")), str(event.get("name")))   # 字符串化:不会抛错
        if event.get("type") == "tool_call":
            pending.setdefault(key, []).append(_as_dict(event.get("args")))  # 同轮按序
        elif event.get("type") == "tool_result":
            queue = pending.get(key) or []
            items.append({"step": event.get("step"), "tool": event.get("name"),
                          "result": event.get("result"),
                          "args": queue.pop(0) if queue else {}})
    items.sort(key=lambda it: (0, it["step"]) if isinstance(it.get("step"), int)
               and not isinstance(it.get("step"), bool) else (1, 0))   # 非整数 step 排末尾
    return items

def _add_node(tree: dict, item: dict, label: str, detail: Any,
              parent: int | None = None) -> int:
    """加节点(索引即下标);parent 为 None = 找不到归属:孤点照画,不假装有父。"""
    tree["nodes"].append({"step": item.get("step"), "tool": item.get("tool"),
                          "label": label, "detail": detail})
    if parent is not None:
        tree["edges"].append((parent, len(tree["nodes"]) - 1))
    return len(tree["nodes"]) - 1

def _index(order: Any, level: Any) -> int:
    """层级字段在维度层级序里的下标;没有该序 / 不在序里 = -1(不可比 -> 不成链)。"""
    return order.index(level) if isinstance(order, (list, tuple)) and level in order else -1

def _chain_parent(tree: dict, args: dict, level_orders: dict | None) -> int | None:
    """下钻链的父节点:同维度 + 同指标 + 层级加深才挂上一个维度节点,否则挂根。
    加深 = 给了层级序时 level 的 index 严格大于上一个 level 的 index(缺序 / 不在序里都
    判不了,不成链);没给层级序时退化为「level 名称不同」这条弱判定(它认不出层级回退,
    真实管线应传层级序)。命中 filters 里的父切片则挂该切片,而非维度节点。"""
    prev, dim, level = tree["prev"], args.get("dimension"), args.get("level")
    if not (prev and dim and dim == prev["dim"] and args.get("metric") == prev["metric"]):
        return tree["root"]
    order = (level_orders or {}).get(dim)       # 缺序 / 不在序里 -> index 都是 -1,不成链
    deeper = (level != prev["level"] if level_orders is None else
              _index(order, level) > _index(order, prev["level"]))
    if not deeper:
        return tree["root"]
    keys = [str(v) for v in (args.get("filters") or {}).values()]
    return next((prev["slices"][k] for k in keys if k in prev["slices"]), prev["node"])

def _push_node(tree: dict, item: dict, level_orders: dict | None = None) -> None:
    """按工具语义建节点:anomaly = 顶层 / contribute = 维度 + 切片节点 / 其余 = 叶子。"""
    tool, raw, args = item.get("tool"), item.get("result"), _as_dict(item.get("args"))
    if tool == _ANOMALY:
        row = _as_dict(raw)
        rate, note = _num(row.get("change_rate")), row.get("note")
        state = {True: "异常", False: "未见异常"}.get(row.get("is_anomaly"), "")
        label = " ".join(p for p in (_text(row.get("metric"), 40), state) if p) \
            or f"异常检测:{_text(raw)}"          # 非结构化 result:照画,原文兜底
        if state and rate is not None:
            label += f" {rate:+.1%}" + ("(窗口命中日历,属预期)" if row.get("is_expected") else "")
        # 每次检测各起一棵树(并列,不嵌在上一次分析下);后续 contribute 的落点跟着它
        tree["root"] = _add_node(tree, item, label, note.strip() if isinstance(note, str)
                                 and note.strip() else None)
        tree["prev"] = None                     # 重新检测 = 打断当前下钻链
    elif tool == _CONTRIBUTE:
        dim, level = args.get("dimension"), args.get("level")
        where = f"{_text(dim, 40)}/{_text(level, 40)}".strip("/") or "下钻"
        metric = _text(args.get("metric"), 30)
        label = f"{metric}·{where}" if metric else where      # 带指标名,读者分得清
        err = raw.get("error") if isinstance(raw, dict) else None
        broken = err is not None or (isinstance(raw, str) and bool(raw.strip()))
        label += _WARN if broken else ""
        detail = (f"工具报错: {_text(err, 100)}" if err is not None else _text(raw, 300)) \
            if broken else (args.get("filters") or None)   # 失败如实标出,不画成正常结果
        call = _add_node(tree, item, label, detail, _chain_parent(tree, args, level_orders))
        slices: dict[str, int] = {}
        for entry in (_as_dict(raw).get("top") or []):
            if isinstance(entry, dict):
                slices[str(entry.get("key"))] = _add_node(tree, item, _slice_label(entry),
                                                          where, call)
        if not broken:   # 失败的维度节点不当父:后续下钻挂根,不挂在错误节点下
            tree["prev"] = {"node": call, "dim": dim, "level": level,
                            "metric": args.get("metric"), "slices": slices}
    else:
        dims, group = args.get("dims"), "整窗"
        group = "、".join(_text(d, 20) for d in dims) if isinstance(dims, list) else group
        label = f"查询 {_text(args.get('metric'), 40)} {group}".strip() if tool == _QUERY else \
            f"分解 {_text(_as_dict(raw).get('target'), 40)}".strip() if tool == _DECOMPOSE else \
            f"{_text(tool, 30) or '未知工具'}:{_text(raw)}"   # 未知工具:普通节点照画
        _add_node(tree, item, label, args.get("filters") or None,
                  tree["prev"]["node"] if tree["prev"] and tool == _QUERY else tree["root"])

def build_tree(drilldown: list[dict], level_orders: dict | None = None) -> dict:
    """下钻链 -> 树结构:{"nodes": [...], "edges": [(父索引, 子索引), ...]}。
    detect_anomaly = 顶层节点(label 带指标名与结论;多次检测 = 并列的多棵树);contribute
    = 维度节点(label = 指标·维度/层级)+ 每个 top 切片;工具报错标 ⚠️ 且 detail 留原文;
    query_metric = 叶子证据节点。边只按事件判定,不猜业务:连续 contribute **同维度 +
    同指标 + 层级加深**才成父子链(回退 / 换指标都挂根);给了 level_orders(={维度: 层级
    字段由粗到细})时按层级 index 严格递增判定,不给则退化为「level 名称不同」的弱判定;
    filters 含父切片 key 时挂该切片,否则挂维度节点下。返回纯 dict(无 streamlit 对象)。"""
    tree = {"nodes": [], "edges": [], "root": None, "prev": None}
    for item in (drilldown if isinstance(drilldown, list) else []):
        if isinstance(item, dict):
            _push_node(tree, item, level_orders)
    return {"nodes": tree["nodes"], "edges": tree["edges"]}

def waterfall_from_contribute(result: dict) -> list[dict]:
    """contribute 结果 -> 瀑布图条目:[{label, base, delta}, ...]。
    首条 = 总基期值(base=total_base, delta=0),每条 top 切片一条(base = 该切片基期值,
    delta = change),末条 = 总对比期值;按 change 升序。只支持 additive(含 change 键);
    补不平账时补一条残差条(标签按截断 / 无明细 / 不可读分三种说法)。"""
    row = _as_dict(result)
    top = row.get("top")
    entries = [e for e in (top or []) if isinstance(e, dict)] if isinstance(top, list) else []
    total_base, total_cmp = _num(row.get("total_base")), _num(row.get("total_cmp"))
    if total_base is None or total_cmp is None \
            or any(_num(e.get("change")) is None for e in entries):
        return []                   # 比率型(derived)不可加 / 切片缺 change:不画,不猜
    change = _num(row.get("total_change")) or (total_cmp - total_base)
    metric = _text(row.get("metric"), 30)
    bars = [{"label": f"{metric} 基期".strip(), "base": total_base, "delta": 0.0}]
    sum_base = sum_delta = 0.0
    for entry in sorted(entries, key=lambda e: _num(e.get("change")) or 0.0):
        base, delta = _num(entry.get("base")) or 0.0, _num(entry.get("change")) or 0.0
        sum_base, sum_delta = sum_base + base, sum_delta + delta
        bars.append({"label": _slice_label(entry), "base": base, "delta": delta})
    scale = _TOL * max(1.0, abs(total_base), abs(total_cmp))
    if abs(total_base - sum_base) > scale or abs(change - sum_delta) > scale:
        # top_k 截断后还有切片没进图 -> 补残差条。省略几条不可知(引擎只回传前 N 条),
        # 故只声明「已画 N 条之外的部分」,绝不编一个数字;另两种情形各说各的。
        unreadable = top is not None and (not isinstance(top, list) or len(entries) != len(top))
        bars.append({"label": _UNREADABLE if unreadable else _NO_SLICE if not entries
                     else f"其余切片(已省略 {len(entries)} 条之外的切片)",
                     "base": total_base - sum_base, "delta": change - sum_delta})
    bars.append({"label": f"{metric} 对比期".strip(), "base": total_cmp, "delta": 0.0})
    return bars

def waterfall_from_decompose(result: dict) -> list[dict]:
    """decompose 结果 -> 瀑布图条目(整窗 effects):首条 = total_base,各 effect 一条
    (label 用 effect 的 label,delta = effect,base = effect 的 base 或 None),末条 =
    total_cmp;带「近似」标注的 label 原样保留(诚实展示,不洗掉近似声明)。"""
    row = _as_dict(result)
    effects, target = row.get("effects"), _text(row.get("target"), 30)
    total_base, total_cmp = _num(row.get("total_base")), _num(row.get("total_cmp"))
    if not isinstance(effects, list) or not effects or total_base is None or total_cmp is None:
        return []
    bars = [{"label": f"{target} 基期".strip(), "base": total_base, "delta": 0.0}]
    for entry in effects:
        item = _as_dict(entry)      # label 原样搬运:带「近似」标注也不洗掉
        effect = _num(item.get("effect"))
        if effect is not None:
            bars.append({"label": _text(item.get("label"), 40)
                         or _text(item.get("factor"), 40) or "因子",
                         "base": _num(item.get("base")), "delta": effect})
    bars.append({"label": f"{target} 对比期".strip(), "base": total_cmp, "delta": 0.0})
    return bars

def render_tree(container, tree: dict) -> None:
    """树 -> container 内的 st.graphviz_chart(DOT 文本由本函数生成;树为空不渲染)。"""
    nodes = (tree.get("nodes") or []) if isinstance(tree, dict) else []
    raw = (tree.get("edges") or []) if isinstance(tree, dict) else []
    if not nodes:
        return
    edges = [(e[0], e[1]) for e in raw if isinstance(e, (tuple, list)) and len(e) == 2
             and all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(nodes)
                     for i in e)]     # 越界 / 非整数索引一律丢弃:画的图不许编造父子
    # 切片节点 = 与父节点同一次 contribute 调用(维度节点是「下钻入口」,底色更深一档)
    clips = {b for a, b in edges if _as_dict(nodes[b]).get("tool") == _CONTRIBUTE
             and _as_dict(nodes[b]).get("step") == _as_dict(nodes[a]).get("step")}
    lines = ['digraph attribution {', '  graph [rankdir=TB, bgcolor="transparent"];',
             '  node [shape=box, style="rounded,filled", fontsize=11, color="#CBD5E1", '
             'fontname="Microsoft YaHei, sans-serif", margin="0.10,0.06"];',
             '  edge [color="#94A3B8", arrowsize=0.7];']
    for index, node in enumerate(nodes):
        entry, tool = _as_dict(node), _as_dict(node).get("tool")
        icon, fill = _STYLE.get(tool, ("•", _FILL_PLAIN))
        text = f"{icon} {entry.get('label') or tool or '?'}"[:_DOT].replace(
            "\\", "\\\\").replace('"', '\\"')           # DOT 字符串转义
        fill = _FILL_DIM if tool == _CONTRIBUTE and index not in clips else fill
        lines.append(f'  n{index} [label="{text}", fillcolor="{fill}"];')
    lines += [f"  n{a} -> n{b};" for a, b in edges]
    lines.append("}")
    container.graphviz_chart("\n".join(lines))

def _waterfall_rows(bars: list[dict]) -> list[dict]:
    """瀑布图条目 -> 每条的显式区间 [{label, y0, y1, kind}](y0 <= y1)。
    累计水平在 Python 里自己算(y0/y1 显式计算,累计为负也正确),不靠 Vega 的 stack:
    累计跌破 0 时正负段不会被拆栈。首末条从 0 画到总量,中间条画 [起点, 起点+delta]。"""
    rows: list[dict] = []
    level, last = 0.0, len(bars) - 1
    for index, bar in enumerate(bars):
        entry = _as_dict(bar)
        base, delta = _num(entry.get("base")) or 0.0, _num(entry.get("delta")) or 0.0
        end = index in (0, last)                 # 首末条:中性,从 0 画到总量
        lo, hi = ((min(0.0, base), max(0.0, base)) if end else
                  (min(level, level + delta), max(level, level + delta)))
        rows.append({"label": _text(entry.get("label"), 60) or f"#{index + 1}", "y0": lo,
                     "y1": hi, "kind": "flat" if end else ("up" if delta >= 0 else "down")})
        level = base if end else level + delta
    return rows

def render_waterfall(container, bars: list[dict], title: str = "贡献度瀑布图") -> None:
    """瀑布图条目 -> container 内的 st.altair_chart:每条用 y / y2 画成显式区间(区间由
    _waterfall_rows 给出),涨绿跌红,首末条中性色;bars 为空不渲染。"""
    if not bars:
        return
    import altair as alt     # 延迟导入:纯数据函数与离线单测不必碰 altair
    rows = _waterfall_rows(bars)
    chart = alt.Chart(alt.Data(values=rows)).mark_bar().encode(
        x=alt.X("label:N", sort=list(dict.fromkeys(r["label"] for r in rows)),
                axis=alt.Axis(labelAngle=0, title=None)),
        y=alt.Y("y0:Q", axis=alt.Axis(title="累计水平")), y2=alt.Y2("y1:Q"),
        color=alt.Color("kind:N", legend=None, scale=alt.Scale(
            domain=["up", "down", "flat"], range=[_UP, _DOWN, _FLAT])))
    container.altair_chart(chart.properties(title=title, height=320), width="stretch")

def render_from_events(container, events: list[dict], level_orders: dict | None = None) -> None:
    """总入口(app/app.py 只调它):事件流 -> 归因树 + 各 contribute/decompose 瀑布图。
    工具报错只给一行说明、不画图;只有确认是 derived 形态(有 change_rate、无 change、
    无 error)才说「比率型不可加」;两者都不是(结果形态未知)也给一行说明、不硬画;
    无可用事件时不渲染任何东西(不抛错)。level_orders 透传给 build_tree(判定下钻链用),
    不给则按 level 名称弱判定。"""
    drilldown = extract_drilldown(events)
    if not drilldown:
        return
    render_tree(container, build_tree(drilldown, level_orders))
    for item in drilldown:
        tool, raw, row = item.get("tool"), item.get("result"), _as_dict(item.get("result"))
        if tool not in (_CONTRIBUTE, _DECOMPOSE):
            continue
        bars = (waterfall_from_contribute(raw) if tool == _CONTRIBUTE
                else waterfall_from_decompose(raw))
        kind = "贡献度" if tool == _CONTRIBUTE else "因子分解"
        name = _text(row.get("metric") or row.get("target"), 30)
        if "error" in row:                      # 工具失败:说失败,不解释成口径问题(优先)
            container.caption(f"该步工具报错:{_text(row.get('error'), 200)}")
        elif bars:
            render_waterfall(container, bars, title=f"{name} {kind}瀑布图".strip())
        elif "change_rate" in row and "change" not in row:
            container.caption(f"`{name}` 为比率型,变化不可加,不画瀑布图。")  # 诚实降级
        else:                                    # 结果形态未知:不硬画,如实给一行说明
            container.caption(f"`{name or kind}` 结果形态无法识别,不画{kind}瀑布图。")
