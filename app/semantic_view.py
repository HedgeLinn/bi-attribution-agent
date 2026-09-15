"""语义层的只读视图:侧边栏「查看指标与维度」展开区。

**只读,不做任何编辑。** 改语义层(尤其改 `expression` 口径)属**破坏性变更**,
必须走 `docs/REUSE_DESIGN.md` §3.6 的治理流程:变更分级 → diff → 版本 bump → 重算受影响 case。
前端编辑会绕过整套治理,所以这里只展示。

M1(§3.2)把语义层升到 schema v2,新增了 `time_aggregation` / `decompositions` /
`name_column` / `time.calendar` / `caveats` 等语义。本模块把它们一并展示出来——
否则「语义层是唯一事实来源」这件事在前端是看不见的。

M2(§3.7)起这个视图跟随**当前选中**的数据集(渲染哪一份由调用方决定),并额外展示
`dataset.yaml` 的元信息(`render_dataset_meta`)——用户得知道自己在选什么(§3.3)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

import streamlit as st

if TYPE_CHECKING:   # 仅用于类型标注:本模块运行时只依赖 streamlit,不 import harness
    from harness.datasets import DatasetInfo

# 指标类型 -> 中文标签
_TYPE_TAG = {
    "additive": "加总",
    "semi_additive": "半可加",
    "derived": "派生",
    "ratio": "比率",
}

# 时间聚合语义 -> 说明。只有 sum 是"默认无害"的,其余(尤其 last)必须显式提示
_AGG_TAG = {
    "sum": "按 sum 聚合",
    "last": "按 last 取期末值",
    "avg": "按 avg 聚合",
    "max": "按 max 聚合",
}

_DEFAULT_AGG = "sum"

# 分解类型 -> 中文标签
_KIND_TAG = {
    "multiplicative": "乘法分解",
    "additive": "加法分解",
    "ratio": "比率分解",
    "structural": "结构分解",
}


def render_semantic_view(sem: Mapping[str, Any]) -> None:
    """渲染语义层摘要。sem 为已解析的 semantic.yaml 内容。"""
    _render_header(sem)
    _render_metrics(sem.get("metrics") or {})
    _render_dimensions(sem.get("dimensions") or {})
    _render_decompositions(sem.get("decompositions") or [])
    _render_calendar(sem.get("time") or {})
    _render_caveats(sem.get("caveats") or [])


def render_dataset_meta(info: "DatasetInfo") -> None:
    """数据集元信息(dataset.yaml 的封面,区别于 semantic.yaml 这张地图)。

    title / description / industry / case_count 是清单里明确「给前端用的」字段(§3.3):
    光看指标清单判断不出「这套数据是不是我要找的那套」。version 一并展示——
    评估结果绑定它(§3.6①),改了造数逻辑历史数字就不可比。
    """
    st.markdown(f"**{info.title}** · `{info.id}`")
    if info.description:
        st.caption(info.description)
    extras = _meta_extras(info)
    if extras:
        st.caption(" · ".join(extras))


def _meta_extras(info: "DatasetInfo") -> list[str]:
    """可选元信息:缺哪个就不显示哪个,不拿占位符凑数。"""
    extras = []
    if info.industry:
        extras.append(f"行业:{info.industry}")
    if info.case_count is not None:
        extras.append(f"评估用例:{info.case_count} 个")
    if info.version:
        extras.append(f"清单版本 v{info.version}")
    return extras


# ---------------------------------------------------------------------------
# 各分区
# ---------------------------------------------------------------------------
def _render_header(sem: Mapping[str, Any]) -> None:
    """数据集标识 + 版本 + 事实表/时间字段。

    版本是评估结果的绑定依据(§3.6①)——改了地图,历史准确率就不可比,所以必须显示出来。
    """
    dataset = sem.get("dataset") or "(未声明 dataset)"
    version = sem.get("dataset_version") or "(未声明版本)"
    schema = sem.get("schema_version") or "?"
    st.caption(f"`{dataset}` · v{version} · schema {schema}")
    st.caption(f"事实表 `{sem.get('fact_table')}` · 时间字段 `{sem.get('date_field')}`")


def _render_metrics(metrics: Mapping[str, Any]) -> None:
    st.markdown("**指标**")
    if not metrics:
        st.caption("(无)")
        return
    for name, m in metrics.items():
        tag = _TYPE_TAG.get(m.get("type", ""), m.get("type", "?"))
        line = f"- `{name}` {m.get('label', '')} · `{m.get('expression', '')}` · _{tag}_"
        agg = m.get("time_aggregation", _DEFAULT_AGG)
        if agg != _DEFAULT_AGG:
            # 非 sum 的聚合语义是"半可加"这类陷阱的所在,单独标出来
            line += f" · ⏱ {_AGG_TAG.get(agg, agg)}"
        st.markdown(line)


def _render_dimensions(dims: Mapping[str, Any]) -> None:
    st.markdown("**维度**")
    if not dims:
        st.caption("(无)")
        return
    for name, d in dims.items():
        hier = " → ".join(d.get("hierarchy") or [])
        line = f"- `{name}` {d.get('label', '')} · 下钻:{hier}"
        if d.get("name_column"):
            line += f" · 显示名 `{d['name_column']}`"
        st.markdown(line)


def _render_decompositions(decompositions: list) -> None:
    """被归因对象永远是顶层指标,派生指标降格为因子(§4.1)。"""
    if not decompositions:
        return
    st.markdown("**分解方式**")
    for d in decompositions:
        kind = _KIND_TAG.get(d.get("kind", ""), d.get("kind", "?"))
        if d.get("kind") == "structural":
            entity = d.get("entity_dimension", "?")
            st.markdown(f"- `{d.get('target')}` 按 `{entity}` 做{kind}")
        else:
            factors = " × ".join(d.get("factors") or [])
            st.markdown(f"- `{d.get('target')}` = {factors} · _{kind}_")


def _render_calendar(time_block: Mapping[str, Any]) -> None:
    """促销日历:命中促销期的"异常"其实是预期脉冲,不该报为业务问题(§4.4)。"""
    promos = (time_block.get("calendar") or {}).get("promos") or []
    if not promos:
        return
    st.markdown("**促销日历**")
    for p in promos:
        span = " ~ ".join(p.get("range") or [])
        note = f" —— {p['note']}" if p.get("note") else ""
        st.markdown(f"- {p.get('name', '')} `{span}`{note}")


def _render_caveats(caveats: list) -> None:
    """口径陷阱:数据本身不可比的地方,提前告诉模型与用户。"""
    if not caveats:
        return
    st.markdown("**口径陷阱**")
    for c in caveats:
        st.markdown(f"- {c}")
