# -*- coding: utf-8 -*-
"""BI 基础查询图型的渲染:柱状图 · 折线图 · 表格,以及它们的**图型切换器**。

`query_metric` 的结果是最普通那类 BI 数据(分类对比 / 时间趋势),以前一张图都不画 ——
这一层把它补上,并让用户**自己切换**用哪种图看:切换只换渲染,不重取数、不花模型钱。

默认图型由数据形态定(见 `chart_plan.auto_choice`),模型给的 `hint` 可覆盖它,
**用户点选永远优先**(控件有值就用控件值)。

表格走 `st.dataframe` 而不是 vega 的 text mark —— 它本来就是这个产品里的表格形态,
也顺带让「默认柱状」不改变既有图表的计数口径。
"""

from __future__ import annotations

from chart_data import rows_from_query
from chart_plan import auto_choice
from chart_theme import CHART_CHOICES, themed

_HEIGHT = 260


def _bar(rows: list[dict]):
    """分类对比:单色柱(颜色走主题的 configure_bar,不进 mark)。"""
    import altair as alt            # 延迟导入:离线单测不必碰 altair
    return alt.Chart(alt.Data(values=rows)).mark_bar().encode(
        x=alt.X("label:N", sort="-y", axis=alt.Axis(labelAngle=0, title=None)),
        y=alt.Y("value:Q", axis=alt.Axis(title=None)),
        tooltip=[alt.Tooltip("label:N", title="标签"),
                 alt.Tooltip("value:Q", title="数值", format=",.0f")])


def _line(rows: list[dict]):
    """时间趋势:主线 + 淡填充(181 个点也不画数据点标记,免得糊成一团)。"""
    import altair as alt
    base = alt.Chart(alt.Data(values=rows))
    return (base.mark_area(opacity=0.07, line=False).encode(
                x=alt.X("label:N"), y=alt.Y("value:Q")) +
            base.mark_line(strokeWidth=1.6).encode(
                x=alt.X("label:N", axis=alt.Axis(labelAngle=0, title=None)),
                y=alt.Y("value:Q", axis=alt.Axis(title=None)),
                tooltip=[alt.Tooltip("label:N", title="标签"),
                         alt.Tooltip("value:Q", title="数值", format=",.0f")]))


def render_query_chart(container, result: dict, dims, key: str,
                       title: str = "查询结果", hint: str | None = None) -> None:
    """query_metric 结果 -> 图型切换器 + 选中图型的图。

    `key` 必须全局唯一(由 `chart_plan.switcher_key` 生成)—— 回放时多条消息连着渲染,
    撞 key 会直接抛 `DuplicateWidgetID`。`rows` 为空时**一个调用都不发**(空输入零渲染)。
    """
    rows = rows_from_query(result, dims)
    if not rows:
        return
    default = hint if hint in CHART_CHOICES else auto_choice(rows)
    picked = container.segmented_control(
        "图型", CHART_CHOICES, default=default, key=key,
        label_visibility="collapsed") or default
    if picked == "表格":
        container.dataframe([{"标签": r["label"], "数值": r["value"]} for r in rows])
        return
    chart = _line(rows) if picked == "折线" else _bar(rows)
    container.altair_chart(
        themed(chart.properties(title=title, height=_HEIGHT)), width="stretch")