"""日历感知的异常检测:纯判定模块,不碰 DuckDB、不做 IO。

对应 docs/REUSE_DESIGN.md §4.4——把 detect_anomaly 从「固定阈值
``abs(change_rate) >= 0.15``」升级为三件事:

1. **促销日历**:命中已知脉冲期的窗口返回 ``is_expected=True``(并在 ``note`` 里点名是哪个
   促销期);促销日**不参与基线估计**——拿大促当基线去比,基线本身就是被污染的。
2. **同星期几基线**:用「近 N 周的同星期几」的中位数作基线,消除周内效应。
3. **稳健统计**:中位数 + MAD(绝对中位差)替代均值 + 标准差,对离群点不敏感。

输入输出都是纯 Python 值:``series`` 是 ``[(日期 'YYYY-MM-DD', 值)]``(按日期升序),
``calendar`` 是语义层 ``time.calendar`` **节点本身**——形状为「日历种类 -> 条目列表」,
本模块**通用遍历**该映射下的所有日历种类、不写死任何日历名(§3.6⑤)。

边界与诚实性(§3.6「不可比就说不可比」):

    - ``series`` 为空 / 对比窗口内无数据 / 基线期无可用样本 -> ``is_anomaly=False``,
      ``base`` 保持 ``None``,**不编基线**(不用 0、也不用邻窗冒充)
    - 历史不足 ``baseline_weeks`` 周(或对比窗口里某个星期几的匹配样本太少)时无法做
      同星期几基线,退化为可比历史整段的中位数;``baseline_type`` **如实报告** ``flat``
      并写进 ``note``,绝不假装用了同星期几
    - 缺失值(``None`` / ``NaN``)被剔除,不当作 0(§5.4 的 C4:没有行不等于 0)
    - 日期无法解析时抛 ``ValueError``:静默跳过会让基线悄悄少样本,比直接失败更危险

``is_anomaly`` 只判「变化率是否超阈值」;MAD 决定的是 ``anomaly_kind``(窗口稳不稳、是否形成新平台)。
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from statistics import median
from typing import Any

__all__ = ["BASELINE_WEEKS_DEFAULT", "assess"]

BASELINE_WEEKS_DEFAULT = 4      # 同星期几基线的默认回看周数(§4.4)
_WEEK_LENGTH = 7                # 一周的天数,只用于回看窗口的日期加减
_MIN_WEEKDAY_SAMPLES = 2        # 单个星期几的最少匹配样本数,少于它不足以谈「同星期几」
_MAD_SCALE = 1.4826             # MAD -> 标准差的一致尺度因子(正态下 MAD × 它 ≈ 标准差)
_TREND_SHARE = 0.5              # 窗口首尾落差占总变化的比例达到它 -> 趋势变化而非台阶
_PLATEAU_MAD_FACTOR = 2.0       # 窗口离散度不超过「基线离散度 × 它」-> 稳定平台(台阶)
_MIN_TREND_POINTS = 4           # 少于该点数不足以谈窗口内的趋势,退回按稳定性判断
_STAMP_FORMAT = "%Y-%m-%d"      # 日期格式:语义层、数据层与工具参数统一用 YYYY-MM-DD
_RATE_DIGITS = 4                # 比率类结果的取位(与 engine._rnd 的默认一致)

# baseline_type 与 anomaly_kind 的取值(冻结的返回契约)
_TYPE_WEEKDAY, _TYPE_FLAT, _TYPE_NONE = "weekday_matched", "flat", "none"
_KIND_PULSE, _KIND_LEVEL_SHIFT, _KIND_TREND_CHANGE = "pulse", "level_shift", "trend_change"

# 各形态的中文解释,以及「超阈值 / 未超阈值」两句兜底话术
_KIND_TEXTS = {
    _KIND_PULSE: "窗口内水平不稳定,判为脉冲(偏离未形成新的稳定水平)",
    _KIND_LEVEL_SHIFT: "窗口是一个稳定平台,判为台阶(水平整体挪了一格)",
    _KIND_TREND_CHANGE: "变化在窗口内逐步累积,判为趋势变化",
}
_ABOVE_THRESHOLD = "变化率超过阈值"
_BELOW_THRESHOLD = "变化率未超过阈值"

# 类型别名:一个数据点 (时刻, 值) 与一个日历窗口 (条目名, 起, 止)
_Point = tuple[dt.datetime, float]
_Span = tuple[str, dt.datetime, dt.datetime]


def assess(series: Sequence[tuple[str, float]], cmp_start: str, cmp_end: str,
           threshold: float, calendar: Mapping[str, Any] | None = None,
           baseline_weeks: int = BASELINE_WEEKS_DEFAULT) -> dict:
    """判定对比窗口相对稳健基线是否异常,并给出形态与是否落在促销日历内。

    参数:
        series: [(日期 'YYYY-MM-DD', 值)],按日期升序;缺失值会被剔除
        cmp_start / cmp_end: 对比窗口(闭区间)
        threshold: 变化率阈值,``abs(change_rate) >= threshold`` 即为 ``is_anomaly``
        calendar: 语义层 time.calendar 节点;``None`` 表示这份数据没有日历知识
        baseline_weeks: 同星期几基线的回看周数
    """
    win_start, win_end = _parse_stamp(cmp_start), _parse_stamp(cmp_end)
    if win_end < win_start:
        raise ValueError(f"对比窗口起止颠倒:{cmp_start} > {cmp_end}")

    spans = _calendar_spans(calendar)
    names = _covering_names(spans, win_start, win_end)
    points = _to_points(series)
    window = [point for point in points if win_start <= point[0] <= win_end]
    if not window:
        return _undecided("对比窗口内没有任何数据点", names)

    prior = [point for point in points if point[0] < win_start]
    weekdays = {moment.weekday() for moment, _ in window}
    samples, baseline_type, dropped = _baseline_samples(
        prior, weekdays, spans, win_start, baseline_weeks)
    if not samples:
        return _undecided("基线期没有可用样本(可能整段落在促销日历内)", names)
    return _result(window, samples, baseline_type, dropped, names, threshold, baseline_weeks)


def _result(window: Sequence[_Point], samples: Sequence[_Point], baseline_type: str,
            dropped: int, names: Sequence[str], threshold: float, weeks: int) -> dict:
    """用稳健基线算变化率与形态,组装返回值(键名见模块文档里的冻结契约)。"""
    values = [value for _, value in window]
    base = float(median([value for _, value in samples]))
    spread = _spread([value for _, value in samples], base)
    cmp_value = float(median(values))
    change = cmp_value - base
    change_rate = round(change / base, _RATE_DIGITS) if base else None
    is_anomaly = bool(change_rate is not None and abs(change_rate) >= threshold)
    kind = _classify(bool(names), is_anomaly, change, spread, values)
    return {
        "base": base,
        "cmp": cmp_value,
        "change": change,
        "change_rate": change_rate,
        "is_anomaly": is_anomaly,
        "baseline_type": baseline_type,
        "is_expected": bool(names),
        "anomaly_kind": kind,
        "note": _note(baseline_type, weeks, names, dropped, kind, is_anomaly,
                      base, spread, change_rate),
        "baseline_mad": spread,
        "robust_z": (round(change / (_MAD_SCALE * spread), _RATE_DIGITS)
                     if spread > 0 else None),
    }


# ---------------------------------------------------------------------------
# 内部实现:解析、日历遍历、基线构造、形态判定、说明文字
# ---------------------------------------------------------------------------
def _parse_stamp(stamp: Any) -> dt.datetime:
    """把 'YYYY-MM-DD'(或带时间的等价写法)解析为当天零点;无法解析时抛 ValueError。

    静默跳过会让基线悄悄少样本,比直接失败更危险。
    """
    return dt.datetime.strptime(str(stamp).strip().split(" ")[0], _STAMP_FORMAT)


def _as_number(value: Any) -> float | None:
    """数值化;``None`` / ``NaN`` / 非数值一律返回 None(缺失,不当作 0)。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _to_points(series: Sequence[tuple[str, float]] | None) -> list[_Point]:
    """series -> [(时刻, 值)],按时刻升序;缺失值剔除而不是补 0。"""
    points: list[_Point] = []
    for stamp, value in series or ():
        number = _as_number(value)
        if number is not None:
            points.append((_parse_stamp(stamp), number))
    points.sort(key=lambda item: item[0])
    return points


def _as_sequence(value: Any) -> list[Any]:
    """YAML 节点 -> 列表;标量 / None / 字符串退化为空列表。"""
    return list(value) if isinstance(value, Sequence) and not isinstance(value, str) else []


def _calendar_spans(calendar: Mapping[str, Any] | None) -> list[_Span]:
    """通用遍历日历节点 -> [(条目名, 起, 止)]。

    节点形状是「日历种类 -> 条目列表」,语义层可自由增加种类(大促 / 节假日 / 停服窗口):
    这里**逐个种类取条目**,既不写死种类名,也不假设只有一种日历。
    """
    if isinstance(calendar, Mapping):
        groups: Sequence[Any] = list(calendar.values())
    elif isinstance(calendar, Sequence) and not isinstance(calendar, str):
        groups = [calendar]      # 也接受「直接给一个条目列表」的简化写法
    else:
        return []
    spans: list[_Span] = []
    for group in groups:
        for entry in _as_sequence(group):
            span = _span_of(entry) if isinstance(entry, Mapping) else None
            if span is not None:
                spans.append(span)
    return spans


def _span_of(entry: Mapping[str, Any]) -> _Span | None:
    """一条日历条目 -> (名字, 起, 止);range 缺失或无法解析时跳过该条。"""
    stamps = _as_sequence(entry.get("range"))
    if not stamps:
        return None
    try:
        start, end = _parse_stamp(stamps[0]), _parse_stamp(stamps[-1])
    except ValueError:
        return None
    return str(entry.get("name") or ""), start, end


def _in_calendar(spans: Sequence[_Span], moment: dt.datetime) -> bool:
    """该时刻是否落在任一日历窗口内(闭区间)。"""
    return any(span_start <= moment <= span_end for _, span_start, span_end in spans)


def _covering_names(spans: Sequence[_Span], start: dt.datetime, end: dt.datetime) -> list[str]:
    """与窗口有交集的日历条目名(去重保序);命中即「变化属预期」。"""
    return list(dict.fromkeys(
        name for name, span_start, span_end in spans
        if span_start <= end and start <= span_end))


def _baseline_samples(prior: Sequence[_Point], weekdays: set[int], spans: Sequence[_Span],
                      win_start: dt.datetime, baseline_weeks: int
                      ) -> tuple[list[_Point], str, int]:
    """构造基线样本 -> (样本, baseline_type, 被剔除的促销样本数)。

    首选同星期几基线:回看 ``baseline_weeks`` 周内,与对比窗口同星期几、且不在日历内的点。
    两种情况**如实退化**为整段中位数(flat),而不是硬凑一个同星期几:
        1. 已有历史没覆盖满整段回看窗口(数据不足 ``baseline_weeks`` 周)
        2. 对比窗口里某个星期几在回看窗口内样本太少(少于 ``_MIN_WEEKDAY_SAMPLES`` 个)
    """
    lookback_start = win_start - dt.timedelta(days=_WEEK_LENGTH * baseline_weeks)
    covered = bool(prior) and prior[0][0] <= lookback_start
    candidates = [point for point in prior
                  if point[0] >= lookback_start and point[0].weekday() in weekdays]
    matched = [point for point in candidates if not _in_calendar(spans, point[0])]
    counts = Counter(moment.weekday() for moment, _ in matched)
    enough = all(counts[weekday] >= _MIN_WEEKDAY_SAMPLES for weekday in weekdays)
    if covered and enough:
        return matched, _TYPE_WEEKDAY, len(candidates) - len(matched)

    fallback = [point for point in prior if not _in_calendar(spans, point[0])]
    return fallback, _TYPE_FLAT, len(prior) - len(fallback)


def _spread(values: Sequence[float], center: float) -> float:
    """MAD(绝对中位差):以中位数为中心的中位数绝对偏差,替代标准差。"""
    return float(median([abs(value - center) for value in values]))


def _trend_drift(values: Sequence[float]) -> float | None:
    """窗口内「后半段中位数 - 前半段中位数」;点数太少时返回 None(不足以谈趋势)。"""
    if len(values) < _MIN_TREND_POINTS:
        return None
    half = len(values) // 2
    return float(median(list(values[half:])) - median(list(values[:half])))


def _classify(is_expected: bool, is_anomaly: bool, change: float,
              base_spread: float, window_values: Sequence[float]) -> str | None:
    """判定异常形态;不构成异常时返回 None。

    - ``pulse``:日历登记过的脉冲期;或窗口内部上下摆动、没形成新的稳定水平
    - ``trend_change``:变化是在窗口内逐步累积的(首尾水平落差占了总变化的大头)
    - ``level_shift``:窗口是一个稳定平台,水平整体挪了一格
    """
    if not is_anomaly:
        return None
    if is_expected:
        return _KIND_PULSE
    drift = _trend_drift(window_values)
    if drift is not None and change and abs(drift) >= _TREND_SHARE * abs(change):
        return _KIND_TREND_CHANGE
    plateau = _spread(window_values, float(median(list(window_values))))
    return _KIND_LEVEL_SHIFT if plateau <= _PLATEAU_MAD_FACTOR * base_spread else _KIND_PULSE


def _undecided(reason: str, names: Sequence[str]) -> dict:
    """数据不足时的返回值:如实说明,**不编基线**(base / cmp 保持 None)。"""
    tail = f";对比窗口命中促销日历:{'、'.join(names)}" if names else ""
    return {
        "base": None, "cmp": None, "change": None, "change_rate": None,
        "is_anomaly": False, "baseline_type": _TYPE_NONE, "is_expected": bool(names),
        "anomaly_kind": None, "baseline_mad": None, "robust_z": None,
        "note": f"{reason},数据不足,不估计基线(不会用 0 或邻窗冒充){tail}",
    }


def _note(baseline_type: str, weeks: int, names: Sequence[str], dropped: int,
          kind: str | None, is_anomaly: bool, base: float, spread: float,
          change_rate: float | None) -> str:
    """中文说明:解释判定依据(用了什么基线、剔除了什么、为什么是这个形态)。"""
    if baseline_type == _TYPE_WEEKDAY:
        parts = [f"基线取近 {weeks} 周同星期几的中位数(已消除周内效应)"]
    else:
        parts = [f"同星期几样本不足 {weeks} 周,基线退化为可比历史整段的中位数(flat)"]
    if dropped:
        parts.append(f"其中 {dropped} 个落在促销日历内的样本已从基线剔除")
    if names:
        parts.append("对比窗口命中促销日历:"
                     + "、".join(names) + ",该变化属预期脉冲,不是业务异常")
    elif is_anomaly:
        parts.append(_KIND_TEXTS.get(kind) or _ABOVE_THRESHOLD)
    else:
        parts.append(_BELOW_THRESHOLD)
    rate = "未定义(基线为 0)" if change_rate is None else f"{change_rate:+.2%}"
    parts.append(f"基线 {base:.6g},MAD {spread:.6g},变化率 {rate}")
    return ";".join(parts)
