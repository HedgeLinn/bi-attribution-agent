# -*- coding: utf-8 -*-
"""图表的视觉基础:配色 token + Altair 主题配置。

两条纪律:

1. **基准色必须与 `app/theme.py` 的 CSS 变量一致**(测试从 `_GLOBAL_CSS` 里抽值比对)。
   改这里等于改设计系统,要两边一起改。
2. **涨跌色是语义色,不随设计系统走**:绿涨红跌 —— 别「顺手对齐」成 indigo。
   只有瀑布图与变化率图用它;分类对比图一律单色 indigo,保持克制。

`themed()` 把外观统一收到 Altair 的 `configure_*` 层,**不写进 mark 参数** ——
这样 `spec["mark"]` 保持 `{"type": "bar"}` 这样的干净形态(有测试精确钉住),
样式也不会散落在每个图里。
"""

from __future__ import annotations

# ---- 基准色(值 = app/theme.py 的 CSS 变量)----
PRIMARY = "#4F46E5"          # --c-primary     indigo
PRIMARY_SOFT = "#EEF2FF"     # --c-primary-soft
SURFACE = "#F1F5F9"          # --c-surface
BORDER = "#E2E8F0"           # --c-border
TEXT = "#0F172A"             # --c-text
MUTED = "#64748B"            # --c-text-muted
BG = "#FFFFFF"               # --c-bg

# ---- 图表语义色(不随设计系统改)----
UP, DOWN, FLAT = "#16A34A", "#DC2626", "#94A3B8"      # 涨(绿)/ 跌(红)/ 中性

# ---- 归因树节点底色(按工具类型分区)----
FILL_ANOMALY = "#FEF3C7"
FILL_CONTRIBUTE = PRIMARY_SOFT
FILL_QUERY = "#F0FDF4"
FILL_DECOMPOSE = "#F5F3FF"
FILL_DIM, FILL_PLAIN = "#E0E7FF", "#F1F5F9"           # 下钻入口 / 未知或降级节点

FONT = '-apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif'

# 图型切换器可选项(顺序即界面顺序);默认项是自动匹配出的那个
CHART_CHOICES = ("柱状", "折线", "表格")


def themed(chart):
    """给顶层 chart 套上统一外观:无边框、浅网格、克制的轴与标题。

    只作用于 `configure_*`(进 spec 的 `config`),不碰 `mark`/`encoding` ——
    所以调用它的图仍保有干净的 mark 形态。
    """
    return (chart
            .configure_view(stroke=None, fill=None)
            .configure_axis(labelFont=FONT, titleFont=FONT, labelColor=MUTED,
                            titleColor=MUTED, gridColor=BORDER, domainColor=BORDER,
                            tickColor=BORDER, labelFontSize=11, titleFontSize=11,
                            domainWidth=1, tickSize=4)
            .configure_title(font=FONT, fontSize=13, fontWeight=600,
                             color=TEXT, anchor="start", offset=12)
            .configure_legend(labelFont=FONT, titleFont=FONT, labelColor=MUTED,
                              titleColor=MUTED, labelFontSize=11)
            # 默认单色:分类对比图一律 indigo(涨跌语义只留给瀑布图与变化率图)。
            # 走 configure 层 —— mark 里不带 colour,`spec["mark"]` 保持干净形态。
            .configure_bar(color=PRIMARY, cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
            .configure_line(color=PRIMARY)
            .configure_area(color=PRIMARY))