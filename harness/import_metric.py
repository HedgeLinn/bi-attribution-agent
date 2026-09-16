"""指标口径的变换原语:把向导的结构化选择拼成语义层字段(纯 dict / 纯文本变换)。

从 harness/import_answers.py 迁出(那边到行数上限了)。这里放**单指标粒度**的操作:
按维度取值拆分、按答案调口径、新增派生比率,以及输出 YAML 的键序约定;
「整份地图怎么组装、答案怎么遍历」留在 import_answers.py。

为什么只认整串恰为 SUM(...) 的表达式:拆分是给聚合加条件,不是给任意表达式加条件。
比率 / 多聚合的表达式拆不出来 —— 显式报 ConfirmError,而不是猜一个看起来对的结果。

本模块零 IO、零 DuckDB、零 Streamlit,可独立单测。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence

from attribution.expression import referenced_identifiers
from harness.importer import ConfirmError

__all__ = [
    "DEFAULT_SEMI_AGG", "METRIC_KEYS", "TYPE_ADDITIVE", "TYPE_DERIVED",
    "TYPE_SEMI_ADDITIVE", "default_split_name", "derived_metrics", "one_split",
    "ordered", "quote_literal", "split_metric", "tune_metric",
]

# 指标类型的三个选项。derived 只用于「分子 / 分母」这一种形态:同粒度派生
# (客单价 = 金额 / 订单数);跨粒度的 ratio(two_stage)不在向导的表达能力内。
TYPE_ADDITIVE = "additive"
TYPE_SEMI_ADDITIVE = "semi_additive"
TYPE_DERIVED = "derived"

# 半可加唯一可选的 time_aggregation:avg / max 的查询语义尚未实现
# (attribution/sql_source.py 遇到会抛 SemanticError)——给用户一个选不动的选项
# 等于给他一个陷阱,所以下拉里只有 last。
DEFAULT_SEMI_AGG = "last"

# 输出 YAML 的键序:与既有数据集的 semantic.yaml 一致(sort_keys=False 会照抄插入序)
METRIC_KEYS = ("label", "unit", "expression", "type", "time_aggregation",
               "depends_on", "source")

# 字符串字面量(含 '' 转义):判括号配平时先掩码掉,避免 '含(括号)的取值' 干扰
_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_ID_TAIL_RE = re.compile(r"[^A-Za-z0-9_]+")


def split_metric(entry: Mapping, base: str, column: str, values: Sequence[str],
                 new_name: str) -> tuple[str, dict]:
    """按某列取值把一个加法指标拆成新指标,返回 (新指标名, 新指标定义)。

        SUM(x) -> SUM(CASE WHEN col = 'v' THEN x ELSE 0 END)      (单取值)
        SUM(x) -> SUM(CASE WHEN col IN ('v1','v2') THEN x ELSE 0 END)

    新指标一律 additive(条件聚合的求和仍是求和),depends_on 由表达式现场推导,
    保证它与实际引用天然一致(§3.5 的硬校验:两者不一致直接判地图不合法)。

    **注意覆盖性**:SQL 里 NULL 不等于任何值,没被勾选的取值(以及 NULL 行)既不属于
    父指标也不属于任何子项,Σ子项 ≠ 父指标。这是口径事实,不是 bug —— 由界面提示,
    不在这里替用户补默认值(见 import_pending.count_uncovered)。
    """
    inner = sum_inner(str(entry.get("expression") or ""))
    if inner is None:
        raise ConfirmError(
            f"只能拆分形如 SUM(列) 的加法指标;{base!r} 的表达式拆不了:"
            f"{entry.get('expression')!r}"
        )
    picked = [str(v) for v in values if str(v) != ""]
    if not picked:
        raise ConfirmError(f"按 {column!r} 拆分至少要勾选一个取值")
    expression = f"SUM(CASE WHEN {condition(column, picked)} THEN {inner} ELSE 0 END)"
    new = {
        "label": str(entry.get("label") or base),
        "expression": expression,
        "type": TYPE_ADDITIVE,
        "depends_on": referenced_identifiers(expression),
    }
    for key in ("unit", "source"):   # 单位与来源表跟着原指标走
        if entry.get(key):
            new[key] = entry[key]
    return new_name, ordered(new, METRIC_KEYS)


def default_split_name(base: str, value: str, index: int) -> str:
    """给拆分出的指标起一个 id 友好的名字。

    指标 key 会进 YAML、进工具 schema 的 enum、进评估报告,所以只留 [A-Za-z0-9_]。
    中文取值(如「华东」)净化后为空 —— 那时退回 `base_<序号>` 而不是造一个空壳名字。
    """
    tail = _ID_TAIL_RE.sub("_", str(value)).strip("_")
    return f"{base}_{tail}" if tail else f"{base}_{index}"


def tune_metric(entry: Mapping, spec: Mapping | None) -> dict:
    """按答案调既有指标的口径:类型 / 时间聚合 / 单位 / 标签。

    depends_on 一律由表达式**重新推导**而不是沿用原值:类型与表达式一旦不同步,
    §3.5 的「depends_on 与表达式引用一致」会直接判地图不合法,现场推导从源头上堵掉它。
    """
    out = copy.deepcopy(dict(entry))
    spec = spec or {}
    kind = str(spec.get("type") or out.get("type") or "").strip()
    if kind in (TYPE_ADDITIVE, TYPE_SEMI_ADDITIVE):
        out["type"] = kind
        if kind == TYPE_SEMI_ADDITIVE:
            # 必须显式声明(§3.5);且只能是 last —— avg / max 尚未实现查询语义
            out["time_aggregation"] = str(
                spec.get("time_aggregation") or out.get("time_aggregation") or DEFAULT_SEMI_AGG)
        else:
            out.pop("time_aggregation", None)   # sum 是默认值,不写
    label = str(spec.get("label") or "").strip()
    if label:
        out["label"] = label
    if spec.get("unit") is not None:
        unit = str(spec["unit"]).strip()
        if unit:
            out["unit"] = unit
        else:
            out.pop("unit", None)               # 清空单位要真的删掉,不能留空串
    out["depends_on"] = referenced_identifiers(str(out.get("expression") or ""))
    return out


def one_split(entry: Mapping, base: str, split: Mapping, index: int) -> tuple[str, dict]:
    """一条拆分声明 -> (新指标名, 定义)。名字缺省时按取值/序号生成。"""
    values = [str(v) for v in (split.get("values") or [])]
    name = str(split.get("name") or "").strip() or default_split_name(
        base, values[0] if values else "", index)
    return split_metric(entry, base, str(split.get("column") or "").strip(), values, name)


def derived_metrics(specs: Sequence, existing: Mapping) -> list[tuple[str, dict]]:
    """新增指标:只支持「分子 / 分母」形(分母自带 NULLIF 除零守卫,§3.1 规则 4)。

    分子分母从**已有指标**里选,不让用户写自由 SQL —— 同粒度派生是结构化可表达的,
    而自由 SQL 会绕过 check_semantic.py 的版本治理(§3.7③)。
    """
    added: list[tuple[str, dict]] = []
    for spec in specs:
        if not isinstance(spec, Mapping):
            continue
        name = str(spec.get("name") or "").strip()
        if not name or name in existing or any(n == name for n, _ in added):
            continue                       # 既有指标的改口径走 tune_metric,这里只收新增
        if spec.get("keep") is False:
            continue
        derived = spec.get("derived") or {}
        numerator = str(derived.get("numerator") or "").strip()
        denominator = str(derived.get("denominator") or "").strip()
        if not numerator or not denominator:
            raise ConfirmError(f"新增指标 {name!r} 需要同时给出分子与分母")
        if numerator == denominator:
            raise ConfirmError(f"新增指标 {name!r} 的分子与分母不能是同一个指标")
        expression = f"{numerator} / NULLIF({denominator}, 0)"
        added.append((name, ordered({
            "label": str(spec.get("label") or "").strip() or name,
            "unit": str(spec.get("unit") or "").strip(),
            "expression": expression,
            "type": TYPE_DERIVED,
            "depends_on": referenced_identifiers(expression),
        }, METRIC_KEYS)))
    return added


def ordered(entry: Mapping, keys: Sequence[str]) -> dict:
    """按约定键序重排:YAML 用 sort_keys=False 输出,插入序就是文件里的行序。

    约定里没有的键追加在后面(不丢字段),空串按「没写」处理 —— 空串进 YAML 会变成
    `unit: ''`,下游要为主张的「没有单位」和「单位是空串」各写一份分支。
    """
    out = {key: entry[key] for key in keys if key in entry and entry[key] != ""}
    out.update({key: value for key, value in entry.items() if key not in out})
    return out


def sum_inner(expr: str) -> str | None:
    """整串恰为 SUM(...) 时返回括号内的原文,否则 None。

    判括号配平前先把字符串字面量掩码成等长空白:取值里带括号(如 'A(旧)')时,
    直接数括号会数错,而掩码保持了下标对齐(与 expression._check_syntax 同一口径)。
    """
    text = expr.strip()
    if text[:4].upper() != "SUM(" or not text.endswith(")"):
        return None
    masked = _LITERAL_RE.sub(lambda m: " " * len(m.group(0)), text)
    depth = 0
    for index in range(3, len(masked)):
        if masked[index] == "(":
            depth += 1
        elif masked[index] == ")":
            depth -= 1
            if depth == 0:
                return text[4:index] if index == len(masked) - 1 else None
    return None


def condition(column: str, values: Sequence[str]) -> str:
    """col = 'v'(单取值)/ col IN ('v1','v2')(多取值);单引号按 SQL 规则转义。"""
    quoted = ", ".join(quote_literal(value) for value in values)
    return f"{column} = {quoted}" if len(values) == 1 else f"{column} IN ({quoted})"


def quote_literal(value: str) -> str:
    """字符串字面量:内部的单引号加倍 —— 取值里带引号时不写这条会拼出非法 SQL。"""
    return "'" + str(value).replace("'", "''") + "'"