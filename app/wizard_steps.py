# -*- coding: utf-8 -*-
"""向导四步的控件与「控件 -> 答案」的收集。

从 app/import_wizard.py 拆出:那边管状态机与落位,这里管「每一步问什么、怎么问」。
控件 key 的命名表在 wizard_common(渲染、记账、回填三处共用),本模块只负责用。

一条贯穿全篇的原则:**state 里没有键 == 用户没渲染过这一步 == 跳过**。于是每个控件都
带一个「草稿原值」当兜底,而缺失即沿用它 —— 界面绝不替用户做一个他没做过的决定。
"""

from __future__ import annotations

import streamlit as st

import wizard_common as wc
import wizard_ledger as wl
from harness.import_answers import apply_answers
from harness.import_confirm import (
    ConfirmError,
    list_level_values,
    preview,
    table_rows,
)
from harness.import_metric import default_split_name, split_metric
from harness.import_pending import PendingPackage

__all__ = ["collect_step", "render_step"]


def render_step(pkg: PendingPackage, step: int) -> None:
    """渲染第 step 步(1..4);越界的 step 按最后一步处理。

    渲染前先把本步上次的选择填回控件(wl.restore),渲染完当场把本步答案记进账本
    (wl.record)—— 为什么两件都不能省,见 wizard_ledger 的模块 docstring。
    """
    keys = wc.step_keys(pkg, step)
    wl.restore(st.session_state, keys)
    if step <= 1:
        _step_identity(pkg)
    elif step == 2:
        _step_metrics(pkg)
    elif step == 3:
        _step_relations(pkg)
    else:
        _step_world(pkg)
    wl.record(st.session_state, keys, collect_step(pkg, st.session_state, step))


# ---------------------------------------------------------------------------
# ① 事实表与时间轴
# ---------------------------------------------------------------------------
def _step_identity(pkg: PendingPackage) -> None:
    """单表导入下事实表没有选择余地(如实说明),真问题是时间轴。"""
    st.markdown("**① 事实表与时间轴**")
    tables = wc.table_names(pkg)
    current = str(pkg.draft.get("fact_table") or "")
    table = st.selectbox("事实表 —— 指标与维度都从它取数", tables or [current],
                         index=(tables.index(current) if current in tables else 0),
                         key=wc.KEY_FACT)
    st.caption(f"自动推断的判据是「行数最多的表」:{table} 有 {table_rows(pkg.data_dir, table)} 行。"
               "本次上传只有一张表,所以这里没有别的选择 —— 多表数据集才会用到它。")

    columns = wc.column_names(pkg)
    date = str(pkg.draft.get("date_field") or "")
    st.selectbox("时间轴 —— 归因窗口、异常检测的基线都按它切分", columns or [date],
                 index=(columns.index(date) if date in columns else 0), key=wc.KEY_DATE)
    st.caption("字符串日期在导入时已转成真正的日期类型;选错会让窗口整段错位。")


# ---------------------------------------------------------------------------
# ② 指标口径(本轮的核心)
# ---------------------------------------------------------------------------
def _step_metrics(pkg: PendingPackage) -> None:
    rows = wc.metric_rows(pkg, st.session_state)
    st.markdown(f"**② 指标口径** · 共 {len(rows)} 条")
    st.caption("自动推断只覆盖「数值列 → SUM」,而且**判错不会报错** —— 把累计型数据判成"
               "半可加,整窗值能差几十倍。逐条过一眼;不要的取消勾选即可。")
    for row in rows:
        _metric_row(pkg, row)
    _preview_box(pkg)


def _metric_row(pkg: PendingPackage, row: dict) -> None:
    """单条指标:保留 / 口径 / 单位 / 拆分。判据为「单调」时默认展开、并说明理由。"""
    key = row["key"]
    with st.expander(f"{row['name']} · `{row['expression']}`", expanded=bool(row["hint"])):
        if row["hint"]:
            st.warning(f"自动推断:{row['hint']}。若这是日流水而不是累计值,请改成加法。")
        labels = [wc.LABEL_ADDITIVE, wc.LABEL_SEMI]
        st.radio("口径", labels, key=f"{key}_type", horizontal=True,
                 index=(labels.index(row["type_label"]) if row["type_label"] in labels else 0),
                 help="加法 = 窗口内逐期求和;半可加 = 只取窗口内最后有数据日的值(余额、MRR 这类时点值)")
        st.text_input("单位(如 元 / 人 / 次)", value=row["unit"], key=f"{key}_unit")
        st.checkbox("保留为指标(取消勾选即从地图里删掉)", value=True, key=f"{key}_keep")
        _split_editor(pkg, row)


def _split_editor(pkg: PendingPackage, row: dict) -> None:
    """按维度取值拆分:一个列 + 一个取值 = 一个业务指标(生成 SUM(CASE WHEN …))。

    本轮唯一能新增口径的能力 ——「同一个金额列按渠道拆成四个流」正是这种形态。
    """
    key = row["key"]
    store = f"{key}_splits"
    done = wc.live_splits(st.session_state, store)
    for index, item in enumerate(done):
        cols = st.columns([5, 1])
        cols[0].caption(f"↳ {item['name']}:限定 {item['column']} ∈ {item['values']}")
        if cols[1].button("删除", key=f"{key}_del{index}"):
            st.session_state[store] = [i for j, i in enumerate(done) if j != index]
            st.rerun()

    column = st.selectbox("拆分裂(留空即不拆)", [""] + wc.column_names(pkg), key=f"{key}_col")
    if not column:
        return
    values = list_level_values(pkg.data_dir, str(pkg.draft.get("fact_table") or ""), column)
    picked = st.multiselect("取值(可多选,合并成一个子项)", values, key=f"{key}_vals")
    new_name = st.text_input("子指标名(留空按「父指标_取值」自动生成)", key=f"{key}_newname")
    if not (picked and st.button("生成子指标", key=f"{key}_add")):
        return
    name = new_name.strip() or default_split_name(row["name"], column, picked)
    try:   # 当场拆一次:拆不出来(表达式不是 SUM(...))要让用户现在就知道,而不是落位时才报错
        split_metric((pkg.draft.get("metrics") or {}).get(row["name"]) or {},
                     row["name"], column, picked, name)
    except ConfirmError as err:
        st.error(f"拆不出来:{err}")
        return
    st.session_state[store] = done + [{"column": column, "values": picked, "name": name}]
    st.rerun()


def _preview_box(pkg: PendingPackage) -> None:
    """两种口径并排看:自动推断 vs 当前答案,**数字来自真引擎**。

    这就是这一步存在的理由 —— 「加法」和「半可加」在界面上只是两个词,而它们的整窗值
    可以差几十倍。不给数字,用户无从判断该不该改自动推断。
    """
    st.divider()
    if not st.button("预览:自动推断 vs 当前答案的整窗值", key="wz_preview",
                     help="用真引擎把每个指标各查一次;这是本步唯一能验证口径对错的证据"):
        return
    answers = {**wl.answers(st.session_state), **collect_step(pkg, st.session_state, 2)}
    with st.spinner("用真引擎各跑一遍…"):
        base = preview(pkg.draft, pkg.data_dir, pkg.root)
        mine = preview(apply_answers(pkg.draft, answers), pkg.data_dir, pkg.root)
    if base.get("error") or mine.get("error"):
        st.warning(f"装载不了:{base.get('error') or mine.get('error')}")
        return
    st.dataframe([{"指标": name, "自动推断": _fmt(base.get(name)),
                   "当前答案": _fmt(mine.get(name)), "倍数": _times(base.get(name), mine.get(name))}
                  for name in sorted(set(base) | set(mine))], hide_index=True)


def _fmt(value) -> str:
    """整窗值 -> 界面文本;取不到或不是数字就如实显示,不拿 0 冒充。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value) if value is not None else "(无)"
    return f"{value:,.4g}"


def _times(base, mine) -> str:
    """当前答案 ÷ 自动推断 —— 这一列才是「口径选错有多贵」的直接证据。"""
    numbers = (base, mine)
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in numbers) and base:
        return f"{mine / base:,.3g}×"
    return "—"


# ---------------------------------------------------------------------------
# ③ 关系与单位
# ---------------------------------------------------------------------------
def _step_relations(pkg: PendingPackage) -> None:
    st.markdown("**③ 关系与单位**")
    st.caption("可下钻列决定模型能不能按这个角度切 —— 没勾的列不会被下钻到。")
    current = [row["column"] for row in wc.dimension_rows(pkg) if row["column"]]
    st.multiselect("可下钻列(低基数文本列)", wc.column_names(pkg),
                   default=current, key=wc.KEY_DIMS)
    st.divider()
    st.caption("恒等式:总量 = 各分量之和(如「收入 = 四个流之和」)。只在真的存在这种关系"
               "时才填 —— 填错会让分解结果自相矛盾;不确定就留空。")
    metrics = sorted(pkg.draft.get("metrics") or {})
    for index in range(wc.DECOMP_ROWS):
        cols = st.columns([2, 3])
        cols[0].selectbox(f"总量 #{index + 1}", [""] + metrics, key=f"wz_dec{index}_target")
        cols[1].text_input(f"分量(逗号分隔) #{index + 1}", key=f"wz_dec{index}_factors")


# ---------------------------------------------------------------------------
# ④ 世界知识
# ---------------------------------------------------------------------------
def _step_world(pkg: PendingPackage) -> None:
    """纯人写的一步:机器对这两项零推断,但它们决定模型会不会把预期内的波动当异常。"""
    st.markdown("**④ 世界知识**")
    st.text_area("口径陷阱(一行一条)", height=120, key=wc.KEY_CAVEATS,
                 placeholder="月度余额是时点值,不能跨月求和\n金额是含税价,与营收口径不同")
    st.caption("这些会写进语义层并出现在模型的上下文里,直接影响它怎么解释数字。")
    st.divider()
    st.caption("促销日历:归因时会把这些区间当作**已知的预期波动**,不再当异常报。"
               "条目一行一条,格式 `名称|起始|终止|备注`(备注可省)。")
    cols = st.columns([1, 3])
    cols[0].text_input("日历名称", key=wc.KEY_CAL_NAME, placeholder="大促")
    cols[1].text_area("日历条目", height=100, key=wc.KEY_CAL_TEXT,
                      placeholder="618|2026-06-01|2026-06-18|大促脉冲\n双11|2026-11-01|2026-11-11")


# ---------------------------------------------------------------------------
# 收集答案:各步现收现记,落位时读账本
# ---------------------------------------------------------------------------
def collect_step(pkg: PendingPackage, state, step: int) -> dict:
    """只收**当前这一步**的答案(为什么不能收全量,见 wizard_ledger 的模块 docstring)。"""
    state = state or {}
    if step <= 1:
        return {"fact_table": str(state.get(wc.KEY_FACT) or ""),
                "date_field": str(state.get(wc.KEY_DATE) or "")}
    if step == 2:
        return {"metrics": _metric_answers(pkg, state)}
    if step == 3:
        answers: dict = {}
        dimensions = state.get(wc.KEY_DIMS)
        if dimensions is not None:             # None == 没渲染过这一步,维度保持原样
            answers["dimensions"] = list(dimensions)
        decompositions = wc.gather_decompositions(state, wc.DECOMP_ROWS)
        if decompositions:
            answers["decompositions"] = decompositions
        return answers
    answers = {}
    calendar = _calendar_answer(state)
    if calendar:
        answers["calendar"] = calendar
    caveats = wc.answer_lines(state.get(wc.KEY_CAVEATS))
    if caveats:
        answers["caveats"] = caveats
    return answers


def _metric_answers(pkg: PendingPackage, state) -> list[dict]:
    """逐条指标答案:保留 / 口径 / 单位 / 拆分。

    type 的兜底是**草稿原值**:state 里没有这个键意味着用户没渲染过这一步,
    此时必须沿用自动推断的类型,而不是默认成加法 —— 那等于界面替他做了个决定。
    """
    draft_metrics = pkg.draft.get("metrics") or {}
    answers = []
    for row in wc.metric_rows(pkg, state):
        key = row["key"]
        entry = draft_metrics.get(row["name"]) or {}
        spec: dict = {
            "name": row["name"],
            "keep": bool(state.get(f"{key}_keep", True)),
            "type": wc.label_to_type(row["type_label"], str(entry.get("type") or "additive")),
            "unit": str(state.get(f"{key}_unit") or ""),
        }
        splits = wc.live_splits(state, f"{key}_splits")
        if splits:
            spec["splits"] = splits
        answers.append(spec)
    return answers


def _calendar_answer(state) -> dict:
    """日历:一个日历名 + 若干条目(名或条目为空就不写,不造默认名)。"""
    name = str(state.get(wc.KEY_CAL_NAME) or "").strip()
    items = wc.answer_pairs(state.get(wc.KEY_CAL_TEXT))
    return {name: items} if name and items else {}