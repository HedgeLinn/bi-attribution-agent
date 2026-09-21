# -*- coding: utf-8 -*-
"""图表的数据层:工具结果 -> 各图型直接消费的行。纯函数,零 streamlit / 零 altair。

从 `attribution_viz.py` 搬入瀑布相关的纯数据函数(**逐字搬运,只去掉函数名的下划线**),
再加上三个新图型的 builder。搬家的理由是行数:渲染与数据混在一个文件里已到 300 行红线。

铁律 ——「**没有数值不等于 0**」:拿不到有限数就**剔除该行**或返回空,
绝不用 0 或邻值冒充。nan / ±inf / 字符串 / bool 一律算「没有数值」。
"""

from __future__ import annotations

import math
from typing import Any

_TEXT_LIMIT = 60                 # 标签默认截断长度
TOL = 1e-12                      # 残差容差(相对总量尺度)
NO_SLICE, UNREADABLE = "无切片明细(仅总量)", "结果不可读"

_KEPT_EVENT_TYPES = ("tool_call", "tool_result")


def as_dict(value: Any) -> dict:
    """非 dict 一律退化成空 dict(事件与结果里什么都可能出现)。"""
    return value if isinstance(value, dict) else {}


def num(value: Any) -> float | None:
    """数值化:bool / None / 字符串 / 非有限(nan/±inf)都算「没有数值」(不猜、不填 0)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    """任意值 -> 单行短文本(None -> 空串);超长截断加省略号。"""
    result = " ".join(str(value).split()) if value is not None else ""
    return result if len(result) <= limit else result[: limit - 1] + "…"


def slice_label(entry: dict) -> str:
    """切片标签 = 切片名 + 变化量(additive 看 change,derived 退到 change_rate);
    数值必须有限(nan / ±inf 只留切片名),绝不产出假的 +nan / +inf 后缀。"""
    name = text(entry.get("key")) or text(entry.get("label"), 40) or "?"
    delta, rate = num(entry.get("change")), num(entry.get("change_rate"))
    delta, rate = [v if v is None or math.isfinite(v) else None for v in (delta, rate)]
    if delta is not None:
        return f"{name} {format(delta, '+,.0f')}"
    return f"{name} {rate:+.1%}" if rate is not None else name


def keep_events(events: Any) -> list[dict]:
    """留存用的事件裁剪:只留 `tool_call` / `tool_result`。

    `final` 与消息里已存的 `content` 重复、`usage` 与三个 token 字段重复,滤掉省约 1/3 体积;
    画图本来也只消费这两类(`extract_drilldown`)。非 dict 的元素直接丢弃 ——
    事件流里什么都可能出现,而它要进 JSON 落盘。
    """
    if not isinstance(events, list):
        return []
    return [e for e in events if isinstance(e, dict) and e.get("type") in _KEPT_EVENT_TYPES]


# ---------------------------------------------------------------------------
# 瀑布图(搬自 attribution_viz,逻辑逐字未动)
# ---------------------------------------------------------------------------
def waterfall_from_contribute(result: dict) -> list[dict]:
    """contribute 结果 -> 瀑布图条目:[{label, base, delta}, ...]。
    首条 = 总基期值(base=total_base, delta=0),每条 top 切片一条(base = 该切片基期值,
    delta = change),末条 = 总对比期值;按 change 升序。只支持 additive(含 change 键);
    补不平账时补一条残差条(标签按截断 / 无明细 / 不可读分三种说法)。"""
    row = as_dict(result)
    top = row.get("top")
    entries = [e for e in (top or []) if isinstance(e, dict)] if isinstance(top, list) else []
    total_base, total_cmp = num(row.get("total_base")), num(row.get("total_cmp"))
    if total_base is None or total_cmp is None \
            or any(num(e.get("change")) is None for e in entries):
        return []                   # 比率型(derived)不可加 / 切片缺 change:不画,不猜
    change = num(row.get("total_change")) or (total_cmp - total_base)
    metric = text(row.get("metric"), 30)
    bars = [{"label": f"{metric} 基期".strip(), "base": total_base, "delta": 0.0}]
    sum_base = sum_delta = 0.0
    for entry in sorted(entries, key=lambda e: num(e.get("change")) or 0.0):
        base, delta = num(entry.get("base")) or 0.0, num(entry.get("change")) or 0.0
        sum_base, sum_delta = sum_base + base, sum_delta + delta
        bars.append({"label": slice_label(entry), "base": base, "delta": delta})
    scale = TOL * max(1.0, abs(total_base), abs(total_cmp))
    if abs(total_base - sum_base) > scale or abs(change - sum_delta) > scale:
        # top_k 截断后还有切片没进图 -> 补残差条。省略几条不可知(引擎只回传前 N 条),
        # 故只声明「已画 N 条之外的部分」,绝不编一个数字;另两种情形各说各的。
        unreadable = top is not None and (not isinstance(top, list) or len(entries) != len(top))
        bars.append({"label": UNREADABLE if unreadable else NO_SLICE if not entries
                     else f"其余切片(已省略 {len(entries)} 条之外的切片)",
                     "base": total_base - sum_base, "delta": change - sum_delta})
    bars.append({"label": f"{metric} 对比期".strip(), "base": total_cmp, "delta": 0.0})
    return bars


def waterfall_from_decompose(result: dict) -> list[dict]:
    """decompose 结果 -> 瀑布图条目(整窗 effects):首条 = total_base,各 effect 一条
    (label 用 effect 的 label,delta = effect,base = effect 的 base 或 None),末条 =
    total_cmp;带「近似」标注的 label 原样保留(诚实展示,不洗掉近似声明)。"""
    row = as_dict(result)
    effects, target = row.get("effects"), text(row.get("target"), 30)
    total_base, total_cmp = num(row.get("total_base")), num(row.get("total_cmp"))
    if not isinstance(effects, list) or not effects or total_base is None or total_cmp is None:
        return []
    bars = [{"label": f"{target} 基期".strip(), "base": total_base, "delta": 0.0}]
    for entry in effects:
        item = as_dict(entry)      # label 原样搬运:带「近似」标注也不洗掉
        effect = num(item.get("effect"))
        if effect is not None:
            bars.append({"label": text(item.get("label"), 40)
                         or text(item.get("factor"), 40) or "因子",
                         "base": num(item.get("base")), "delta": effect})
    bars.append({"label": f"{target} 对比期".strip(), "base": total_cmp, "delta": 0.0})
    return bars


def waterfall_rows(bars: list[dict]) -> list[dict]:
    """瀑布图条目 -> 每条的显式区间 [{label, y0, y1, kind}](y0 <= y1)。
    累计水平在 Python 里自己算(y0/y1 显式计算,累计为负也正确),不靠 Vega 的 stack:
    累计跌破 0 时正负段不会被拆栈。首末条从 0 画到总量,中间条画 [起点, 起点+delta]。"""
    rows: list[dict] = []
    level, last = 0.0, len(bars) - 1
    for index, bar in enumerate(bars):
        entry = as_dict(bar)
        base, delta = num(entry.get("base")) or 0.0, num(entry.get("delta")) or 0.0
        end = index in (0, last)                 # 首末条:中性,从 0 画到总量
        lo, hi = ((min(0.0, base), max(0.0, base)) if end else
                  (min(level, level + delta), max(level, level + delta)))
        rows.append({"label": text(entry.get("label"), 60) or f"#{index + 1}", "y0": lo,
                     "y1": hi, "kind": "flat" if end else ("up" if delta >= 0 else "down")})
        level = base if end else level + delta
    return rows


# ---------------------------------------------------------------------------
# 三个新图型的数据(本次新增)
# ---------------------------------------------------------------------------
def rows_from_query(result: dict, dims: Any) -> list[dict]:
    """query_metric 结果 -> [{label, value}, ...](柱状/折线/表格共用)。

    标签取 `dims` 里**实际出现在行上的**字段值,多字段用 ` / ` 连接;
    `value` 不是有限数的行直接剔除 —— 不拿 0 补位。
    """
    rows = as_dict(result).get("rows")
    if not isinstance(rows, list):
        return []
    keys = [d for d in (dims if isinstance(dims, list) else []) if isinstance(d, str)]
    out: list[dict] = []
    for item in rows:
        entry = as_dict(item)
        value = num(entry.get("value"))
        if value is None:
            continue
        parts = [text(entry.get(k), 40) for k in keys if entry.get(k) is not None]
        label = " / ".join(p for p in parts if p) or text(entry.get("key"), 40) or "?"
        out.append({"label": label, "value": value})
    return out


def bars_from_contribute_rate(result: dict) -> list[dict]:
    """比率型 contribute 结果 -> 变化率条形数据 [{label, rate}, ...]。

    比率型不可加(没有 `change`),画不了瀑布 —— 但它**有**逐切片的 `change_rate`,
    画横向条形看谁跌得最狠是诚实的表达。收不到任何有限变化率就返回空(不画)。
    """
    top = as_dict(result).get("top")
    if not isinstance(top, list):
        return []
    out: list[dict] = []
    for item in top:
        entry = as_dict(item)
        rate = num(entry.get("change_rate"))
        if rate is None:
            continue
        out.append({"label": text(entry.get("label") or entry.get("key"), 40) or "?", "rate": rate})
    return out


def bands_from_anomaly(result: dict) -> list[dict] | None:
    """detect_anomaly 结果 -> 基线 vs 实际两条 [{kind, value, lo, hi}, ...]。

    **基线不可估计时返回 None**(`base` 为 None,`baseline_type="none"`)——
    「没有行不等于 0」,不拿 0 冒充基线。MAD 缺失或为 0 时不编造带宽(lo == hi)。
    灰线画的是基线 ±MAD:比两根裸柱子多一层「这个变化算不算异常」的判据。
    """
    row = as_dict(result)
    base, current = num(row.get("base")), num(row.get("cmp"))
    if base is None or current is None:
        return None
    mad = num(row.get("baseline_mad")) or 0.0
    return [{"kind": "统计基线", "value": base, "lo": base - mad, "hi": base + mad},
            {"kind": "对比窗口", "value": current, "lo": current, "hi": current}]