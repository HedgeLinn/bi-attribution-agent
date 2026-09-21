# -*- coding: utf-8 -*-
"""归因类图型的渲染:瀑布图 · 变化率条形图 · 基线对比图。

只做渲染 —— 数据换算在 `chart_data`(纯函数),配色与主题在 `chart_theme`。
三个函数都遵守同一条形态纪律:**mark 里不写样式参数**,配色走 `color` 通道、
主题走 `configure_*`。这样 `spec["mark"]` 保持 `{"type": "bar"}` 的干净形态
(有测试精确钉住),样式也不会散落到每个图里。

`render_waterfall` 是**逐字搬移**(只把 `_waterfall_rows` 改成 `waterfall_rows`),
它是「搬运而非重写」的验收锚点 —— 别顺手美化。
"""

from __future__ import annotations

from chart_data import bands_from_anomaly, waterfall_rows
from chart_theme import DOWN, FLAT, MUTED, PRIMARY, UP, themed


def render_waterfall(container, bars: list[dict], title: str = "贡献度瀑布图") -> None:
    """瀑布图条目 -> container 内的 st.altair_chart:每条用 y / y2 画成显式区间(区间由
    waterfall_rows 给出),涨绿跌红,首末条中性色;bars 为空不渲染。"""
    if not bars:
        return
    import altair as alt     # 延迟导入:纯数据函数与离线单测不必碰 altair
    rows = waterfall_rows(bars)
    chart = alt.Chart(alt.Data(values=rows)).mark_bar().encode(
        x=alt.X("label:N", sort=list(dict.fromkeys(r["label"] for r in rows)),
                axis=alt.Axis(labelAngle=0, title=None)),
        y=alt.Y("y0:Q", axis=alt.Axis(title="累计水平")), y2=alt.Y2("y1:Q"),
        color=alt.Color("kind:N", legend=None, scale=alt.Scale(
            domain=["up", "down", "flat"], range=[UP, DOWN, FLAT])))
    # 也走 themed():搬运时曾保持原样,结果是同页里只有瀑布图用的是另一套字体/网格/轴色
    container.altair_chart(
        themed(chart.properties(title=title, height=320)), width="stretch")


def render_change_rate(container, bars: list[dict], title: str = "变化率") -> None:
    """比率型切片 -> 横向条形图。

    比率型指标不可加,画不了瀑布图 —— 但它**有**逐切片的 `change_rate`,
    看谁跌得最狠是它能诚实支撑的图。颜色按正负(涨绿跌红),条形为空不渲染。
    """
    if not bars:
        return
    import altair as alt
    chart = alt.Chart(alt.Data(values=bars)).mark_bar().encode(
        y=alt.Y("label:N", sort="x", axis=alt.Axis(title=None)),
        x=alt.X("rate:Q", axis=alt.Axis(title="环比变化率", format="%")),
        color=alt.Color("rate:Q", legend=None, scale=alt.Scale(
            domain=[-0.2, 0, 0.2], range=[DOWN, FLAT, UP])),
        tooltip=[alt.Tooltip("label:N", title="切片"),
                 alt.Tooltip("rate:Q", title="变化率", format="+.1%")])
    height = max(120, 34 * len(bars) + 70)          # 每条一行,条少也不至于扁
    container.altair_chart(
        themed(chart.properties(title=title, height=height)), width="stretch")


def render_baseline(container, result: dict, title: str = "基线 vs 实际") -> None:
    """detect_anomaly 结果 -> 基线/实际两根柱 + 基线 ±MAD 的离散带。

    灰带是「这个变化算不算异常」的判据(比两根裸柱子多一层信息)。
    **基线不可估计时不画**(`bands_from_anomaly` 返回 None,`baseline_type="none"`)——
    与「不拿 0 冒充基线」同一条纪律;MAD 缺失时自动少画那两层。
    """
    bands = bands_from_anomaly(result)
    if bands is None:
        return
    import altair as alt
    base = alt.Chart(alt.Data(values=bands))
    chart = base.mark_bar(size=64).encode(
        x=alt.X("kind:N", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("value:Q", axis=alt.Axis(title=None)),
        color=alt.Color("kind:N", legend=None, scale=alt.Scale(
            domain=["统计基线", "对比窗口"], range=[FLAT, PRIMARY])),
        tooltip=[alt.Tooltip("kind:N", title="口径"),
                 alt.Tooltip("value:Q", title="数值", format=",.0f")])
    if any(item["hi"] > item["lo"] for item in bands):      # 有离散带才画带
        chart = chart + base.mark_rule(color=MUTED, strokeWidth=1.4, opacity=0.85).encode(
            x=alt.X("kind:N"), y=alt.Y("lo:Q"), y2=alt.Y2("hi:Q"))
    container.altair_chart(
        themed(chart.properties(title=title, height=260)), width="stretch")