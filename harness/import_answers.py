"""向导答案 -> 语义层 dict(docs/REUSE_DESIGN.md §6.2 B 档的落地映射)。

这一层只管一件事:**把人在前端做的结构化选择翻译成语义层字段**。它是纯 dict 变换 ——
零 IO、零 DuckDB、零 Streamlit,因此可以独立单测,也不必担心「界面改了但地图没改」。

为什么必须有这层:harness/importer.py 自动推断出来的地图能覆盖「数值列 -> SUM」,
覆盖不了只能人写的业务语义 —— 半可加判定(月度余额不是日流水)、按维度取值拆出的
业务指标(一个列 + 一个取值 = 一个指标)、分子分母口径、分解恒等式、促销日历与口径陷阱。
实测:月度锯齿数据(跨月归零)的两个指标都被自动判成 semi_additive + last,
整窗值 128 vs 逐日真值 9,618,而生成的 YAML 完全合法、装载校验一声不吭。

结构化选择而不是自由 SQL:改 expression 属破坏性变更,必须走 scripts/check_semantic.py
的版本治理;前端给一个表达式输入框就等于把治理旁路了(§3.7③「编辑——不做」)。

单指标的变换原语(拆分 / 调口径 / 派生 / 键序)在 harness/import_metric.py,
本模块负责「整份地图怎么组装」。

答案的形状(向导与测试都按它构造;字段缺省 = 跳过该步 = 沿用自动推断值):

    {
      "fact_table": "fact", "date_field": "date",
      "dimensions": ["region"],                      # 可下钻列(派生维度),替换自动推断
      "metrics": [                                   # 列表:顺序即输出顺序
        {"name": "amount_sum", "keep": True, "type": "additive",
         "unit": "元", "label": "金额",
         "splits": [{"column": "channel", "values": ["抖音"], "name": "amount_sum_douyin"}]},
        {"name": "aov", "type": "derived",           # 新增:分子 / 分母都取已有指标
         "derived": {"numerator": "amount_sum", "denominator": "orders_count"}},
      ],
      "decompositions": [{"target": "mrr", "kind": "additive", "factors": ["a", "b"]}],
      "caveats": ["月末余额是时点值,不能跨月求和"],
      "calendar": {"大促": [{"name": "618", "range": ["2026-06-01", "2026-06-18"],
                             "note": "大促期的波动是预期脉冲"}]},
    }
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import date

from harness.import_metric import (   # 前两个是再导出:调用方不必知道它们被拆到了哪里
    METRIC_KEYS,
    default_split_name,               # noqa: F401
    derived_metrics,
    one_split,
    ordered,
    split_metric,                     # noqa: F401
    tune_metric,
)
from harness.importer import ConfirmError

__all__ = [
    "ConfirmError", "apply_answers", "default_split_name", "split_metric",
]

# 分解声明的键序(与既有数据集的 semantic.yaml 一致)
_DECOMP_KEYS = ("target", "kind", "factors")


def apply_answers(draft: Mapping, answers: Mapping | None) -> dict:
    """草稿 + 向导答案 -> 最终语义层 dict。不落盘、不校验(校验交给 verify)。

    答案里没提到的字段一律沿用草稿 —— 每一步都能跳过,跳过 == 不写任何字段。
    """
    out = copy.deepcopy(dict(draft))
    ans = answers or {}
    _apply_identity(out, ans)
    if ans.get("metrics") is not None:
        out["metrics"] = _apply_metrics(out, list(ans["metrics"]))
    if ans.get("dimensions") is not None:
        out["dimensions"] = _apply_dimensions(out, ans["dimensions"])
    if ans.get("decompositions") is not None:
        out["decompositions"] = _apply_decompositions(list(ans["decompositions"]))
    if ans.get("caveats") is not None:
        out["caveats"] = _clean_lines(ans["caveats"])
    if ans.get("calendar") is not None:
        _apply_calendar(out, ans["calendar"])
    return out


def _apply_identity(out: dict, ans: Mapping) -> None:
    """事实表与时间轴。

    换事实表会把「用默认事实表」的那些指标一起带过去,所以先把它们的 source 清掉
    ——本轮只有一张表可挂,source 指认留给多表轮次。
    """
    fact = str(ans.get("fact_table") or "").strip()
    if fact and fact != out.get("fact_table"):
        for entry in (out.get("metrics") or {}).values():
            entry.pop("source", None)
        out["fact_table"] = fact
    date_field = str(ans.get("date_field") or "").strip()
    if date_field:
        out["date_field"] = date_field


def _apply_metrics(out: dict, specs: list) -> dict:
    """逐条应用指标答案:保留 / 删掉 / 改口径 / 按维度取值拆分 / 新增派生比率。

    specs 里没提到的既有指标原样保留 —— 向导只列它认识的,不该顺手删掉其余的。
    """
    base = out.get("metrics") or {}
    known = {str(s.get("name")): s for s in specs
             if isinstance(s, Mapping) and s.get("name")}
    metrics: dict = {}
    for name, entry in base.items():
        spec = known.get(name)
        if spec is not None and spec.get("keep") is False:
            continue
        metrics[name] = tune_metric(entry, spec)
        for index, split in enumerate(list((spec or {}).get("splits") or []), start=1):
            new_name, new_entry = one_split(metrics[name], name, split, index)
            _reject_duplicate(metrics, new_name)
            metrics[new_name] = new_entry
    for new_name, new_entry in derived_metrics(specs, metrics):
        _reject_duplicate(metrics, new_name)
        metrics[new_name] = new_entry
    return {name: ordered(item, METRIC_KEYS) for name, item in metrics.items()}


def _reject_duplicate(metrics: Mapping, name: str) -> None:
    """重名即报错:同名指标会静默覆盖,而覆盖掉的往往正是用户刚调好的那一条。"""
    if name in metrics:
        raise ConfirmError(f"指标 {name!r} 重名了:请换一个名字(拆分默认名可能撞车)")


def _apply_dimensions(out: dict, chosen) -> dict:
    """可下钻列:向导给的是「事实表派生维度」的列名清单,整体替换草稿里的派生维度。

    表型维度(多表场景)原样保留 —— 本轮没有,但替换逻辑不该把它们一起吃掉。
    """
    dims = {name: dim for name, dim in (out.get("dimensions") or {}).items()
            if str(dim.get("type")) != "derived"}
    for column in chosen or []:
        name = str(column).strip()
        if name:
            dims[name] = {"label": name, "type": "derived", "hierarchy": [name]}
    return dims


def _apply_decompositions(specs: list) -> list:
    """additive 恒等式(target = Σ factors,saas 的「MRR = 四个流之和」形态)。

    只组装结构,合法性交给 verify:target / factors 必须是已有指标、因子个数下限、
    ratio 恰两个因子,这些规则地图层已经有一份(find_decomposition_problems),不重写。
    填了一半的行当没填(不写进地图),而不是写一份注定不合法的声明让用户去猜错在哪。
    """
    result = []
    for spec in specs:
        if not isinstance(spec, Mapping):
            continue
        target = str(spec.get("target") or "").strip()
        factors = [str(f).strip() for f in (spec.get("factors") or []) if str(f).strip()]
        if not target or not factors:
            continue
        result.append(ordered({"target": target,
                               "kind": str(spec.get("kind") or "additive").strip(),
                               "factors": factors}, _DECOMP_KEYS))
    return result


def _apply_calendar(out: dict, calendar: Mapping) -> None:
    """促销日历:日历名自由填,条目只保留四要素齐全的。

    语义层 time.calendar 是自由形状(日历种类 -> 条目列表),消费方按名遍历,
    所以这里不预设任何日历名 —— 名称为空的行直接丢弃,不造默认名。

    range 必须是 ISO 日期:anomaly._span_of 对解析不了的日期是**静默跳过**的,
    写进去等于把用户填错的那一行悄悄吞掉,所以这里显式报错。
    """
    entries: dict = {}
    for calendar_name, items in (calendar or {}).items():
        rows = []
        for item in items or []:
            name = str(item.get("name") or "").strip()
            span = [str(part).strip() for part in (item.get("range") or [])][:2]
            if not name or len(span) != 2 or not all(span):
                continue                   # 起止不全的条目进地图只会误导模型
            row = {"name": name, "range": _iso_span(span, str(calendar_name or ""))}
            note = str(item.get("note") or "").strip()
            if note:
                row["note"] = note
            rows.append(row)
        key = str(calendar_name or "").strip()
        if key and rows:
            entries[key] = rows
    if entries:
        out["time"] = {**(out.get("time") or {}), "calendar": entries}
    else:
        out.pop("time", None)


def _iso_span(span: Sequence[str], calendar_name: str) -> list[str]:
    """校验起止都是 YYYY-MM-DD 并归一成 ISO 文本(消费方按 date.fromisoformat 解析)。"""
    normalized = []
    for part in span:
        try:
            normalized.append(date.fromisoformat(part).isoformat())
        except ValueError as err:
            raise ConfirmError(
                f"日历 {calendar_name!r} 的日期 {part!r} 不是 YYYY-MM-DD 格式") from err
    return normalized


def _clean_lines(values) -> list[str]:
    """多行文本 -> 去空行的字符串列表(caveats 是 list[str],不是一整段文本)。

    context._read_caveats 对**字符串**是静默忽略的:写成一整段文本等于没写,
    而界面不会报错。这里统一成列表,从源头上堵掉那种沉默的失效。
    """
    if isinstance(values, str):
        values = values.splitlines()
    lines = []
    for value in values or []:
        text = "" if value is None else str(value).strip()
        if text:
            lines.append(text)
    return lines