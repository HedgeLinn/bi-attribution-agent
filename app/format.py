# -*- coding: utf-8 -*-
"""展示辅助与工具结果摘要:数字 / 百分比 / 参数 / 各工具结果压成 markdown。

从 app.py 拆出,只为把入口文件压在 300 行约束内。这些函数只吃 dict、吐 str,
不持有数据集知识(例外见各函数 docstring),可独立测试。
"""

import json


def _fmt_num(v):
    """数字美化:大数加千分位;None / NaN 显示为 '-'。"""
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:  # NaN
        return "-"
    if abs(f) >= 1000:
        return f"{f:,.0f}"
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}"


def _fmt_pct(r):
    """比率/贡献度(小数形式)转百分比显示,如 -0.2 -> '-20.0%'。"""
    if r is None:
        return "-"
    try:
        v = float(r)
    except (TypeError, ValueError):
        return str(r)
    return f"{v * 100:+.1f}%"


def _fmt_args(args):
    """工具调用参数 -> 紧凑一行文本。"""
    if not args:
        return ""
    return "，".join(f"`{k}`={v}" for k, v in args.items())


def _fmt_detect(d):
    """detect_anomaly 结果摘要。"""
    demo = "✅ 是" if d.get("is_anomaly") else "❌ 否"
    return (
        f"指标 `{d.get('metric')}`:基准期 `{_fmt_num(d.get('base'))}` → "
        f"对比期 `{_fmt_num(d.get('cmp'))}`;变化 `{_fmt_num(d.get('change'))}`,"
        f"变化率 `{_fmt_pct(d.get('change_rate'))}`;是否异常:**{demo}**"
    )


def _fmt_contribute(d):
    """contribute(贡献度下钻)结果摘要:整体变化 + Top 切片表格。"""
    lines = [
        f"指标 `{d.get('metric')}`,下钻维度 `{d.get('dimension')} → {d.get('level')}`",
        f"整体变化 `{_fmt_num(d.get('total_change'))}`"
        f"(基准期 `{_fmt_num(d.get('total_base'))}` → 对比期 `{_fmt_num(d.get('total_cmp'))}`)",
    ]
    top = d.get("top") or []
    if top:
        header = "| 切片 | 基准期 | 对比期 | 变化 | 变化率 | 贡献度 |"
        sep = "|---|---|---|---|---|---|"
        body = []
        for s in top[:6]:
            body.append(
                "| {} | {} | {} | {} | {} | {} |".format(
                    s.get("label") or s.get("key") or "?",
                    _fmt_num(s.get("base")),
                    _fmt_num(s.get("cmp")),
                    _fmt_num(s.get("change")),
                    _fmt_pct(s.get("change_rate")),
                    _fmt_pct(s.get("contribution")),
                )
            )
        if len(top) > 6:
            body.append(f"| … 共 {len(top)} 个切片 | | | | | |")
        lines.append("**Top 切片贡献度**")
        lines.append("\n".join([header, sep] + body))
    return "\n\n".join(lines)


def _fmt_query_metric(d):
    """query_metric 结果摘要:汇总值 + 前几行切片。"""
    lines = []
    metric = d.get("metric", "")
    total = d.get("total")
    rows = d.get("rows") or []
    if total is not None:
        lines.append(f"指标 `{metric}` 整窗汇总 → **{_fmt_num(total)}**")
    shown = []
    for r in rows[:6]:
        dim_val = next((str(r[k]) for k in r if k != "value"), "-")
        shown.append(f"- {dim_val}: {_fmt_num(r.get('value'))}")
    if rows:
        lines.append("\n".join(shown))
        if len(rows) > 6:
            lines.append(f"- … 共 {len(rows)} 行")
    return "\n\n".join(lines) or "*(空结果)*"


def _summarize_result(name, result):
    """把工具返回结果压成一小段 markdown,避免整颗 dict 倾倒。"""
    if isinstance(result, str):
        body = result.strip()
        return body[:600] + ("…" if len(body) > 600 else "")
    if isinstance(result, dict):
        if "error" in result:
            return f"⚠️ 工具报错:`{result['error']}`"
        if name == "detect_anomaly":
            return _fmt_detect(result)
        if name == "contribute":
            return _fmt_contribute(result)
        if name == "query_metric":
            return _fmt_query_metric(result)
        # 兜底:只挑对用户有意义的短字段,不把整个 dict 倒出来
        keep = {k: v for k, v in result.items()
                if k in ("metric", "total", "count", "change", "change_rate")}
        s = json.dumps(keep if keep else result, ensure_ascii=False)
        return s[:600] + ("…" if len(s) > 600 else "")
    return str(result)[:600]
