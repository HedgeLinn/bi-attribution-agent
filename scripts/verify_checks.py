"""`verify_dataset.py` 的两条回验通道(§5.10 约束 4:可回验)。

放在 `verify_dataset.py` 之外的唯一原因:**单文件行数约束(≤300)**。
两条通道都只走 `AttributionEngine` 公开方法(离线可跑,不需 BI_API_KEY),
**不复制造数脚本的任何逻辑**——「以为埋了其实没埋」只有靠独立口径才能发现。
"""

from __future__ import annotations

from typing import Any

from attribution.engine import AttributionEngine
from scripts.eval_common import positive

# check.type 的取值(冻结)
CHECK_METRIC_CHANGE = "metric_change"
CHECK_SLICE_ABSENT = "slice_absent"
CHECK_APPEARED = "appeared"


def run_expectation(engine: AttributionEngine, item: dict) -> dict:
    """跑一条期望:通过 / 不通过 + 实测值;回验本身出错也算不通过(并说明原因)。"""
    check = item["check"]
    try:
        if check["type"] == CHECK_METRIC_CHANGE:
            passed, detail = check_metric_change(engine, check)
        elif check["type"] == CHECK_APPEARED:
            passed, detail = check_appeared(engine, check)
        else:
            passed, detail = check_slice_absent(engine, check)
    except Exception as err:   # noqa: BLE001  回验失败 ≠ 数据没问题,如实记不通过
        passed, detail = False, f"回验执行失败:{type(err).__name__}: {err}"
    return {"id": item["id"], "description": item["description"],
            "passed": passed, "detail": detail}


def check_appeared(engine: AttributionEngine, check: dict) -> tuple[bool, str]:
    """appeared:某条流(带过滤的指标)在基准期没有、在出现期出现了。

    用于「以前没有、现在出现」的埋点(如新出现的流失流)——metric_change 对
    基准期为 0 / 无行的情况按设计判「无定义」,这一类埋点需要这条通道。
    判定:基准期 total 为 None 或 0,且出现期 total > 0。
    """
    base = _metric_total(engine, check["metric"], check["filters"], *check["base"])
    cmp_value = _metric_total(engine, check["metric"], check["filters"], *check["window"])
    detail = f"base={base if base is not None else '无数据'} cmp={cmp_value}"
    if base not in (None, 0):
        return False, detail + f"——基准期已有值 {base},不是「新出现」"
    if not cmp_value or cmp_value <= 0:
        return False, detail + "——出现期没有正值,观测不到这条流"
    return True, detail + "——基准期无值、出现期有值,符合「新出现」"


def check_metric_change(engine: AttributionEngine, check: dict) -> tuple[bool, str]:
    """metric_change:两段窗口的指标变化率是否落在声明区间内、方向是否一致。

    走 query_metric(metric, [], filters, start, end).total——derived 指标同样适用
    (引擎内部先算底层指标再相除),不需要复制造数脚本的任何逻辑。
    """
    base = _metric_total(engine, check["metric"], check["filters"], *check["base"])
    cmp_value = _metric_total(engine, check["metric"], check["filters"], *check["cmp"])
    if base is None or cmp_value is None:
        return False, f"窗口无数据(base={base}, cmp={cmp_value}),无法算变化率"
    if base == 0:
        return False, f"基准期指标为 0(实际 {base}),变化率无定义"
    rate = (cmp_value - base) / base
    problems = []
    if check["direction"] == "down" and not rate < 0:
        problems.append("要求 down 但变化率非负")
    if check["direction"] == "up" and not rate > 0:
        problems.append("要求 up 但变化率非正")
    if check["min_rate"] is not None and abs(rate) < check["min_rate"]:
        problems.append(f"|变化率| < min_rate {check['min_rate']:.2%}")
    if check["max_rate"] is not None and abs(rate) > check["max_rate"]:
        problems.append(f"|变化率| > max_rate {check['max_rate']:.2%}")
    detail = (f"base={base:,.2f} cmp={cmp_value:,.2f} 变化率={rate:+.2%}"
              f"(要求 {'/'.join(_rate_requirement(check))})")
    return (not problems), detail if not problems else detail + ";" + ";".join(problems)


def check_slice_absent(engine: AttributionEngine, check: dict) -> tuple[bool, str]:
    """slice_absent:该切片在窗口内是否已观测不到(下架类埋点的反向检测)。

    过滤键取**维度层级字段名**(level),而不是维度名——语义层里
    `dimension=product` 的切片键是 `product_id`,filter 也必须用字段名。
    实现完全不认识具体数据集:字段名一律来自期望声明。
    """
    filters = {**check["filters"], check["level"]: check["key"]}
    result = engine.query_metric(check["metric"], [check["level"]], filters, *check["window"])
    rows = result.get("rows") or []
    observed = [row for row in rows if positive(row.get("value"))]
    window = f"{check['window'][0]}~{check['window'][1]}"
    if observed:
        values = "、".join(f"{row.get('value'):,.2f}" for row in observed)
        return False, (f"{check['key']} 在 {window} 仍可观测:{check['metric']}={values}"
                       f"(共 {len(observed)} 行)")
    return True, (f"{check['key']} 在 {window} 未观测到 {check['metric']}"
                  f"(引擎返回 {len(rows)} 行,非零行 0 行)")


def _rate_requirement(check: dict) -> list[str]:
    """把阈值声明还原成人读的要求(只用于 detail 文案)。"""
    parts = []
    if check["direction"]:
        parts.append(check["direction"])
    if check["min_rate"] is not None:
        parts.append(f"|率|≥{check['min_rate']:.2%}")
    if check["max_rate"] is not None:
        parts.append(f"|率|≤{check['max_rate']:.2%}")
    return parts or ["只记录变化率"]


def _metric_total(engine: AttributionEngine, metric: str, filters: dict,
                  start: str, end: str) -> float | None:
    """指标在窗口内的标量值(dims 为空时 query_metric 的 total 才有意义)。"""
    result = engine.query_metric(metric, [], dict(filters), start, end)
    value = result.get("total")
    return None if value is None else float(value)
