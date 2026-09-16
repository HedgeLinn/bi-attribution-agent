# -*- coding: utf-8 -*-
"""导入确认向导:上传之后、落位之前的 4 步人工确认(docs/REUSE_DESIGN.md §6.2 B 档)。

为什么要有它:harness/importer.py 的自动推断是**唯一一条自动产出正式语义层的路径**,
而它的推断能力有明确边界。实测:月度锯齿数据(月内累计、跨月归零)整列被判成
semi_additive + last,引擎整窗值 128 vs 逐日真值 9,618,而生成的 YAML 完全合法、
装载校验一声不吭 —— 这类错机器自己发现不了,只能由人指出来。

与 §3.7③「编辑——不做」的关系:向导管的是**地图的诞生**,不是**地图的修改**。
准入门槛是「这份地图从未经过人工确认」(清单里的 unconfirmed 标记);一旦确认,
入口消失,前端回到只读。已发布数据集的语义层永远不从这里改。

三个模块的分工:
    app/wizard_common.py   纯逻辑(零 Streamlit):答案成形、判据、类型标签
    app/wizard_steps.py    四步的控件与收集
    app/import_wizard.py   状态机 + 导航 + 落位(本模块)

包的状态只活在 session_state 里(wizard_id / wizard_mode / wizard_step + wz_* 控件键),
没有「待确认列表」这种 UI:向导中断即遗忘,超时由 cleanup_stale 兜底。
"""

from __future__ import annotations

import streamlit as st

import wizard_ledger
import wizard_steps
from harness.datasets import DEFAULT_DATASETS_DIRNAME
from harness.import_confirm import (
    ConfirmError,
    commit,
    discard,
    list_unconfirmed,
    reconfirm,
    skip,
)
from harness.import_pending import PendingPackage, load_pending, load_placed

__all__ = ["has_wizard", "render_wizard_area", "start_wizard"]

# 向导自身的状态(包的 id / 来源 / 当前步 / 数据集根目录 / 跨 rerun 的回执)
_STATE_ID = "wizard_id"
_STATE_MODE = "wizard_mode"
_STATE_STEP = "wizard_step"
_STATE_DIR = "wizard_dir"
_STATE_FLASH = "wizard_flash"

# 控件键的公共前缀:退出向导时按它整片清理(见 _close)
_PREFIX = "wz_"

_STEPS = 4
_MODE_RECONFIRM = "reconfirm"


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------
def start_wizard(dataset_id: str, mode: str = "new",
                 datasets_dir: str | None = None) -> None:
    """进入向导(上传成功后 / 点「补确认」时由调用方触发)。

    进入前**清空上一次向导的全部状态**:控件 key 是按指标下标命名的(wz_m0_type),
    换一个包进来不清理,上一个包的选择会原样套到新包的指标上 —— 那是最坏的一类错:
    用户没看过的选择,以他的名义写进了地图。
    """
    _close()
    st.session_state[_STATE_ID] = dataset_id
    st.session_state[_STATE_MODE] = mode
    st.session_state[_STATE_STEP] = 1
    if datasets_dir:
        st.session_state[_STATE_DIR] = str(datasets_dir)


def has_wizard() -> bool:
    """是否有正在进行的向导(调用方据此决定主区域让不让位)。"""
    return bool(st.session_state.get(_STATE_ID))


def render_wizard_area(datasets_dir: str | None = None) -> bool:
    """主区域:有向导就渲染向导,返回 True(调用方据此让出主区域);否则只回执。

    返回值而不是内部 st.stop():该不该让出主区域是调用方的排版决定,向导不替它决定。
    """
    _flash()
    package = _current(datasets_dir or _datasets_dir())
    if package is None:
        return False
    _render(*package)
    return True


def _flash() -> None:
    """回执上一次落位 / 放弃的结果,并清掉 —— 它是跨 rerun 传一句话的通道。"""
    message = st.session_state.pop(_STATE_FLASH, "")
    if message:
        st.success(message)


def _current(datasets_dir: str) -> tuple[PendingPackage, str] | None:
    """按 state 加载当前包;包没了(超时被清理 / 已被确认)就收摊并说明原因。"""
    dataset_id = str(st.session_state.get(_STATE_ID) or "")
    if not dataset_id:
        return None
    mode = str(st.session_state.get(_STATE_MODE) or "new")
    try:
        package = (load_placed if mode == _MODE_RECONFIRM else load_pending)
        return package(dataset_id, datasets_dir), mode
    except ConfirmError as err:
        _close()
        st.warning(f"向导已结束:{err}")
        return None


def _close() -> None:
    """退出向导:连同全部 wz_ 前缀的控件键一起清掉(下次进来必须从零开始)。"""
    for key in [key for key in list(st.session_state) if str(key).startswith(_PREFIX)]:
        st.session_state.pop(key, None)
    for key in (_STATE_ID, _STATE_MODE, _STATE_STEP, _STATE_DIR):
        st.session_state.pop(key, None)


def _datasets_dir() -> str:
    """本次向导的数据集根目录(由入口写进 state;缺省即默认 datasets/)。"""
    return str(st.session_state.get(_STATE_DIR) or DEFAULT_DATASETS_DIRNAME)


# ---------------------------------------------------------------------------
# 渲染:标题 + 当前步 + 导航
# ---------------------------------------------------------------------------
def _render(pkg: PendingPackage, mode: str) -> None:
    step = max(1, min(int(st.session_state.get(_STATE_STEP) or 1), _STEPS))
    title = "补确认语义层" if mode == _MODE_RECONFIRM else "确认导入的数据集"
    st.subheader(f"{title}:{pkg.title}")
    st.caption(f"数据集 `{pkg.id}` · 第 {step} / {_STEPS} 步 · 每一步都可以跳过"
               "(跳过即沿用自动推断值,不写任何字段)")
    wizard_steps.render_step(pkg, step)
    _nav(pkg, step, mode)


def _nav(pkg: PendingPackage, step: int, mode: str) -> None:
    """导航行:上一步 / 下一步 / 确认并落位 / 跳过确认 / 放弃。"""
    st.divider()
    cols = st.columns(4)
    if cols[0].button("← 上一步", disabled=step <= 1, key="wz_prev"):
        st.session_state[_STATE_STEP] = step - 1
        st.rerun()
    if cols[1].button("下一步 →", disabled=step >= _STEPS, key="wz_next"):
        st.session_state[_STATE_STEP] = step + 1
        st.rerun()
    if cols[2].button("确认并落位", type="primary", key="wz_commit"):
        _land(pkg, mode, confirmed=True)
    if cols[3].button("跳过确认" if mode != _MODE_RECONFIRM else "保持未确认",
                      key="wz_skip", help="按自动推断的地图落位,并标记为「未确认」"):
        _land(pkg, mode, confirmed=False)
    if mode != _MODE_RECONFIRM and st.button("放弃本次导入(删掉待确认包)", key="wz_drop"):
        _drop(pkg)


# ---------------------------------------------------------------------------
# 落位 / 放弃
# ---------------------------------------------------------------------------
def _land(pkg: PendingPackage, mode: str, confirmed: bool) -> None:
    """把当前答案交给 harness 落位;失败时**不落位**,报错让用户改答案重试。

    「跳过确认」不是绕过闸门:harness 那边同样跑 verify + smoke,只是不打人工确认的标记。
    """
    datasets_dir = _datasets_dir()
    answers = wizard_ledger.answers(st.session_state)
    try:
        if mode == _MODE_RECONFIRM:
            if not confirmed:
                raise ConfirmError("补确认只有「确认」一条路:这份地图已经在数据集里了")
            reconfirm(pkg.id, answers, datasets_dir)
            message = f"数据集 {pkg.id} 的语义层已人工确认(数据未改动)"
        elif confirmed:
            commit(pkg.id, answers, datasets_dir)
            message = f"数据集 {pkg.id} 已确认并落位,可以在左侧选择了"
        else:
            skip(pkg.id, datasets_dir)
            message = f"数据集 {pkg.id} 已按自动推断落位(标记为未确认,可随时回来补确认)"
    except ConfirmError as err:
        st.error(str(err))
        return
    _close()
    st.session_state[_STATE_FLASH] = message
    st.rerun()


def _drop(pkg: PendingPackage) -> None:
    """放弃本次导入:待确认包直接删掉(它的数据只在暂存区里,没有别的副本)。"""
    discard(pkg.id, _datasets_dir())
    _close()
    st.session_state[_STATE_FLASH] = f"已放弃导入 {pkg.id}"
    st.rerun()


# ---------------------------------------------------------------------------
# 补确认入口(决策 6 的「以后可以回来补确认」)
# ---------------------------------------------------------------------------
def render_reconfirm_entry(datasets_dir: str | None = None) -> None:
    """列出「跳过确认」落位的数据集,各给一个补确认按钮(侧边栏导入区调用)。

    这不是被排除掉的「待确认列表」:它列的是**已经能分析**的数据集,只是地图没过人工
    闸门。用户不点它,一切照常 —— 它只是把「可以回来补」这件事变得看得见。
    """
    directory = datasets_dir or _datasets_dir()
    infos = list_unconfirmed(directory)
    if not infos:
        return
    st.caption(f"⚠️ {len(infos)} 个数据集的语义层未经人工确认(按自动推断落位)")
    for info in infos:
        cols = st.columns([3, 1])
        cols[0].caption(f"· {info.title}")
        if cols[1].button("补确认", key=f"wz_re_{info.id}", help=info.id):
            start_wizard(info.id, _MODE_RECONFIRM, directory)
            st.rerun()