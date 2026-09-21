# -*- coding: utf-8 -*-
"""渲染计划层:画什么、画几张、控件叫什么名字 —— 全是纯函数,零 streamlit。

单独成层的两个理由:

- **渲染计划可离线测**:「超限时该折叠几张、展开后剩几张」是逻辑,不是界面。
- **控件键名只有一处产生**:界面渲染与测试都要用同一份命名。键撞车的后果是
  streamlit 直接抛 `DuplicateWidgetID`,而且**回放场景最容易撞** —— 历史消息
  一条接一条渲染,任何「只按图序号命名」的方案都会在第二条消息上炸。

`scope` = 消息 id(老消息没有 id 时由调用方退化成消息序号)。
"""

from __future__ import annotations

import re

CHART_LIMIT = 24        # 可见图数上限:超过则分级渲染(只画最近这些 + 一个展开按钮)


def switcher_key(scope, index: int) -> str:
    """图型切换器的 widget key(scope 内第 index 张图)。"""
    return f"viz_sw_{scope}_{index}"


def expand_key(scope) -> str:
    """「展开更早的图」按钮的 widget key。"""
    return f"viz_expand_btn_{scope}"


def expand_state_key(scope) -> str:
    """「已展开」标志在 session_state 里的键(与 widget key 分开:按钮是一次性的)。"""
    return f"viz_expand_{scope}"


def plan_tasks(tasks: list, expanded: bool, limit: int | None = None) -> tuple[list, int]:
    """渲染计划:(可见任务, 被折叠的数量)。

    未展开且超限时**保留最近的** limit 张 —— 用户刚问的那几轮最相关,老图折叠。
    `limit=None` 时读模块级 `CHART_LIMIT`(便于测试 monkeypatch)。
    """
    cap = CHART_LIMIT if limit is None else max(0, limit)
    items = list(tasks)
    if expanded or len(items) <= cap:
        return items, 0
    # cap 为 0 时 `items[-0:]` 会取回**全部**(Python 的负零切片陷阱):显式分开写
    visible = items[len(items) - cap:] if cap else []
    return visible, len(items) - cap


_TIME_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def looks_like_time(rows: list) -> bool:
    """行标签是否都像 ISO 日期 —— 决定默认画折线还是柱状。

    刻意不看语义层:渲染层不认识数据集(那是不变量),只按数据形态判断。
    """
    labels = [str(r.get("label", "")) for r in rows if isinstance(r, dict)]
    return bool(labels) and all(_TIME_LIKE.match(label) for label in labels)


def auto_choice(rows: list) -> str:
    """按数据形态选默认图型:时间序列画折线、分类对比画柱状;少于两个点退回表格。"""
    if len(rows) < 2:
        return "表格"
    return "折线" if looks_like_time(rows) else "柱状"