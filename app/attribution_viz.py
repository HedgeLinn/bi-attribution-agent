"""归因树 + 图型调度:把 agent 的下钻路径画成图,并决定每步结果该画什么。

**拆分后的职责边界**(为压回行数红线,数据与渲染已外移):
    - 这里:树的纯数据(`build_tree` / `extract_drilldown`)+ 树渲染 + 图型**调度**
    - `chart_data`:各图型的数据(瀑布相关纯函数逐字搬走)
    - `chart_attr` / `chart_basic`:归因图型 / BI 基础图型的渲染
    - `chart_theme`:配色 token;`chart_plan`:控件键名与折叠计划

调度对新模块用**函数内延迟 import** —— 模块级 import 会与 `chart_attr` 形成循环
(它要用这里的瀑布纯数据,而这里要调它的渲染)。

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

from typing import Any

from chart_data import (as_dict, num, slice_label,   # 纯数据层(不依赖 streamlit)
                        text)
from chart_theme import (FILL_ANOMALY, FILL_CONTRIBUTE, FILL_DECOMPOSE, FILL_DIM,
                         FILL_PLAIN, FILL_QUERY)

__all__ = ["build_tree", "extract_drilldown", "render_from_events", "render_tree"]

# 渲染常量:工具名(harness 对模型的稳定契约)/ 图标 / 标记;**无任何数据集词汇**。
_ANOMALY, _CONTRIBUTE, _QUERY, _DECOMPOSE = (
    "detect_anomaly", "contribute", "query_metric", "decompose")
_STYLE = {_ANOMALY: ("🔍", FILL_ANOMALY), _CONTRIBUTE: ("📊", FILL_CONTRIBUTE),
          _QUERY: ("🧮", FILL_QUERY), _DECOMPOSE: ("🧩", FILL_DECOMPOSE)}
_DOT = 120                                       # DOT 标签截断长度
_WARN = " ⚠️"                                    # 工具失败标记:报错绝不画成正常结果

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
            pending.setdefault(key, []).append(as_dict(event.get("args")))  # 同轮按序
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
    tool, raw, args = item.get("tool"), item.get("result"), as_dict(item.get("args"))
    if tool == _ANOMALY:
        row = as_dict(raw)
        rate, note = num(row.get("change_rate")), row.get("note")
        state = {True: "异常", False: "未见异常"}.get(row.get("is_anomaly"), "")
        label = " ".join(p for p in (text(row.get("metric"), 40), state) if p) \
            or f"异常检测:{text(raw)}"          # 非结构化 result:照画,原文兜底
        if state and rate is not None:
            label += f" {rate:+.1%}" + ("(窗口命中日历,属预期)" if row.get("is_expected") else "")
        # 每次检测各起一棵树(并列,不嵌在上一次分析下);后续 contribute 的落点跟着它
        tree["root"] = _add_node(tree, item, label, note.strip() if isinstance(note, str)
                                 and note.strip() else None)
        tree["prev"] = None                     # 重新检测 = 打断当前下钻链
    elif tool == _CONTRIBUTE:
        dim, level = args.get("dimension"), args.get("level")
        where = f"{text(dim, 40)}/{text(level, 40)}".strip("/") or "下钻"
        metric = text(args.get("metric"), 30)
        label = f"{metric}·{where}" if metric else where      # 带指标名,读者分得清
        err = raw.get("error") if isinstance(raw, dict) else None
        broken = err is not None or (isinstance(raw, str) and bool(raw.strip()))
        label += _WARN if broken else ""
        detail = (f"工具报错: {text(err, 100)}" if err is not None else text(raw, 300)) \
            if broken else (args.get("filters") or None)   # 失败如实标出,不画成正常结果
        call = _add_node(tree, item, label, detail, _chain_parent(tree, args, level_orders))
        slices: dict[str, int] = {}
        for entry in (as_dict(raw).get("top") or []):
            if isinstance(entry, dict):
                slices[str(entry.get("key"))] = _add_node(tree, item, slice_label(entry),
                                                          where, call)
        if not broken:   # 失败的维度节点不当父:后续下钻挂根,不挂在错误节点下
            tree["prev"] = {"node": call, "dim": dim, "level": level,
                            "metric": args.get("metric"), "slices": slices}
    else:
        dims, group = args.get("dims"), "整窗"
        group = "、".join(text(d, 20) for d in dims) if isinstance(dims, list) else group
        label = f"查询 {text(args.get('metric'), 40)} {group}".strip() if tool == _QUERY else \
            f"分解 {text(as_dict(raw).get('target'), 40)}".strip() if tool == _DECOMPOSE else \
            f"{text(tool, 30) or '未知工具'}:{text(raw)}"   # 未知工具:普通节点照画
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
    clips = {b for a, b in edges if as_dict(nodes[b]).get("tool") == _CONTRIBUTE
             and as_dict(nodes[b]).get("step") == as_dict(nodes[a]).get("step")}
    lines = ['digraph attribution {', '  graph [rankdir=TB, bgcolor="transparent"];',
             '  node [shape=box, style="rounded,filled", fontsize=11, color="#CBD5E1", '
             'fontname="Microsoft YaHei, sans-serif", margin="0.10,0.06"];',
             '  edge [color="#94A3B8", arrowsize=0.7];']
    for index, node in enumerate(nodes):
        entry, tool = as_dict(node), as_dict(node).get("tool")
        icon, fill = _STYLE.get(tool, ("•", FILL_PLAIN))
        dot_label = f"{icon} {entry.get('label') or tool or '?'}"[:_DOT].replace(
            "\\", "\\\\").replace('"', '\\"')           # DOT 字符串转义
        fill = FILL_DIM if tool == _CONTRIBUTE and index not in clips else fill
        lines.append(f'  n{index} [label="{dot_label}", fillcolor="{fill}"];')
    lines += [f"  n{a} -> n{b};" for a, b in edges]
    lines.append("}")
    container.graphviz_chart("\n".join(lines))

def _expand(scope: str) -> None:
    """「展开更早的图」按钮的回调:置标志位,下一次 rerun 就全渲染(不用回调做重活)。"""
    import streamlit as st
    from chart_plan import expand_state_key
    st.session_state[expand_state_key(scope)] = True


def _render_step(container, item: dict, scope: str, chart_hint: str | None,
                 index: int) -> None:
    """渲染**一步**工具结果(图或一行说明)。分派规则见 `render_from_events`。

    延迟 import 新模块:模块级 import 会与 `chart_attr` 成环(它要用本模块的纯数据)。
    """
    from chart_attr import render_baseline, render_change_rate, render_waterfall
    from chart_basic import render_query_chart
    from chart_data import (bars_from_contribute_rate, waterfall_from_contribute,
                            waterfall_from_decompose)
    from chart_plan import switcher_key

    tool, raw = item.get("tool"), item.get("result")
    row, args = as_dict(raw), as_dict(item.get("args"))
    if tool == _ANOMALY:                        # 基线 vs 实际(基线估不出时不画)
        render_baseline(container, raw,
                        title=f"{text(row.get('metric'), 30)} 基线 vs 实际".strip())
    elif tool == _QUERY:                        # BI 基础图型 + 用户切换器
        render_query_chart(container, raw, args.get("dims"),
                           key=switcher_key(scope, index),
                           title=f"{text(row.get('metric'), 30)} 查询结果".strip(),
                           hint=chart_hint)
    elif tool in (_CONTRIBUTE, _DECOMPOSE):
        kind = "贡献度" if tool == _CONTRIBUTE else "因子分解"
        name = text(row.get("metric") or row.get("target"), 30)
        bars = (waterfall_from_contribute(raw) if tool == _CONTRIBUTE
                else waterfall_from_decompose(raw))
        if "error" in row:                      # 工具失败:说失败,不解释成口径问题(优先)
            container.caption(f"该步工具报错:{text(row.get('error'), 200)}")
        elif bars:
            render_waterfall(container, bars, title=f"{name} {kind}瀑布图".strip())
        elif "change_rate" in row and "change" not in row:
            # 比率型:变化不可加 -> 不画瀑布,改画变化率(它有的就是逐切片变化率)
            container.caption(f"`{name}` 为比率型,变化不可加,改为看变化率。")
            rate_bars = bars_from_contribute_rate(raw)
            if rate_bars:
                render_change_rate(container, rate_bars,
                                   title=f"{name} 各切片变化率".strip())
        else:                                    # 结果形态未知:不硬画,如实给一行说明
            container.caption(f"`{name or kind}` 结果形态无法识别,不画{kind}瀑布图。")


def render_from_events(container, events: list[dict], level_orders: dict | None = None,
                       scope: str = "live", chart_hint: str | None = None,
                       expanded: bool = False) -> None:
    """总入口(当轮与回放都走这里):事件流 -> 归因树 + 各步结果的图。

    **渲染顺序是契约**:树恒第一个(测试与前端都按此假设),随后按事件顺序逐个结果分派。

    `scope` 命名控件 key(回放传消息 id —— 多条消息连着渲染,撞 key 会抛
    DuplicateWidgetID);`chart_hint` 是模型给的默认图型,只决定切换器初始选中项,
    用户点选始终优先。无可用事件时**一个渲染调用都不发**(不抛错)。

    `expanded=False` 且步数超过 `chart_plan.CHART_LIMIT` 时**分级渲染**:只画**最近**的
    若干步,更早的折成一行 + 一个展开按钮。未执行的渲染分支在 streamlit 里不产生任何
    前端开销 —— 这是「不砍图但也不拖慢页面」的实现方式(与 expander 折叠有本质区别:
    后者内容照样渲染)。
    """
    from chart_plan import expand_key, plan_tasks

    drilldown = extract_drilldown(events)
    if not drilldown:
        return
    render_tree(container, build_tree(drilldown, level_orders))

    steps = [(i, item) for i, item in enumerate(drilldown)
             if item.get("tool") in (_ANOMALY, _QUERY, _CONTRIBUTE, _DECOMPOSE)]
    visible, hidden = plan_tasks(steps, expanded)
    for index, item in visible:              # index 是**原始**序号:折叠前后 key 都稳定
        _render_step(container, item, scope, chart_hint, index)
    if hidden:
        container.caption(f"更早的 {hidden} 步图已折叠（点右侧按钮展开）")
        container.button("显示更早的图", key=expand_key(scope), on_click=_expand,
                         args=(scope,))
