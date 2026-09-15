# -*- coding: utf-8 -*-
"""数据集选择器:侧边栏下拉 + 选择驱动的整链重建(docs/REUSE_DESIGN.md §3.7)。

**为什么这不是一个普通的下拉框**:切换数据集 = 重建整条链,不只是换数据源。

    | 连带换掉 | 为什么 |
    |---|---|
    | 数据源 | 指向另一份 data/*.parquet |
    | 工具 schema 的 enum | contribute 的 metric/dimension 取值域来自语义层 |
    | 系统提示词的数据集上下文 | 指标清单、维度层级、时间范围、促销日历、口径陷阱 |
    | 历史对话 | 不该串味:切到别的数据集还看得见上一个数据集的归因结论是错的 |

所以本模块做三件事:

    ① 选择   —— 列出 datasets/*/(发现与解析交给 harness.datasets,唯一事实来源),
                用 dataset.yaml 的 title 显示;只有一个数据集时也照样显示,管路现在就得通。
    ② 重建   —— 选中即调 harness.tools.init_engine(数据目录, 语义层路径),一次重建
                (Semantic → Engine → 工具 schema → 数据集上下文)。loop.run 每次运行都按
                当前语义层现场 build_tools() 并渲染系统提示词,下一次分析用的必然是切换后的链。
    ③ 不串味 —— 会话按数据集**分区**(见 visible_sessions),切回去历史还在。

边界:本模块只回答「选哪个数据集、把哪条链指过去」,不做任何分析计算;
语义层的渲染交给 semantic_view,数据集的发现/解析交给 harness.datasets。
"""

import os
import sys

import streamlit as st
import yaml

# 与前缀 app.py / harness/run.py 同一套定位方式:项目根 = 本文件所在目录的上一级
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
for _path in (ROOT, APP_DIR):   # 独立被 import(如验证脚本)时也要能找到 harness 与 semantic_view
    if _path not in sys.path:
        sys.path.insert(0, _path)

from harness.datasets import (  # noqa: E402
    DEFAULT_DATASETS_DIRNAME,
    DatasetError,
    DatasetInfo,
    discover_datasets,
    resolve_dataset,
)
from harness.tools import init_engine  # noqa: E402
from semantic_view import render_dataset_meta, render_semantic_view  # noqa: E402

__all__ = [
    "available_datasets", "current_dataset", "current_dataset_id", "default_dataset",
    "ensure_engine", "migrate_legacy_sessions", "render_selector", "render_semantic_panel",
    "selection_error", "visible_sessions",
]

# 数据集包所在目录(绝对路径:streamlit 的启动目录不保证是项目根)
DATASETS_DIR = os.path.join(ROOT, DEFAULT_DATASETS_DIRNAME)

# 选择器控件:label 是给用户看的,key 是给测试/调试定位用的(不依赖元素下标)
SELECTOR_LABEL = "分析语义"
SELECTOR_KEY = "dataset_selector"
SELECTOR_HELP = ("切换数据集会重建整条分析链:数据源、工具的可选值、系统提示词"
                 "与历史对话都会跟着换。")

# session_state 键:当前选中的数据集(DatasetInfo),以及最近一次切换的初始化错误
SELECTED_KEY = "selected_dataset"
SELECTION_ERROR_KEY = "dataset_selection_error"

# 会话记录里标记归属数据集的字段名(旧记录没有这个字段,见 _legacy_dataset_id)
SESSION_DATASET_FIELD = "dataset_id"

# 进程级账本:harness.tools 的引擎是**单例全局**,这里记录它当前指向哪个数据集。
# 用模块级而非 session_state,是因为它描述的正是「进程内那唯一一个引擎指向谁」——
# 与 harness.tools 的单例约束对齐(那是既有约束,不是本模块引入的)。
_active_dataset_id = None


# ---------------------------------------------------------------------------
# 数据集清单
# ---------------------------------------------------------------------------
def available_datasets() -> list[DatasetInfo]:
    """可选数据集,按 id 排序。

    磁盘上没有数据集包(全是显式路径场景)时,退化为 resolve_dataset() 解析出的那一个——
    下拉里至少有一个可选,而不是空控件。

    异常:
        DatasetError: 一个数据集都没有,或某个 dataset.yaml 非法(配置错误该被看见)。
    """
    found = discover_datasets(DATASETS_DIR)
    return found or [resolve_dataset(datasets_dir=DATASETS_DIR)]


def default_dataset() -> DatasetInfo:
    """默认数据集:优先用 resolve_dataset() 的解析结果。

    这样「只有一个数据集」「BI_DATASET 指定了数据集」这两种零配置场景与 CLI 完全一致
    (harness/run.py 用的是同一个入口)。有多个数据集又没指定时由下拉框兜底选第一个。

    异常:
        DatasetError: 一个数据集都没有。
    """
    try:
        return resolve_dataset(datasets_dir=DATASETS_DIR)
    except DatasetError:
        found = discover_datasets(DATASETS_DIR)
        if not found:
            raise
        return found[0]


def current_dataset() -> DatasetInfo | None:
    """当前选中的数据集;选择器还没渲染过时按默认规则解析一次。"""
    stored = st.session_state.get(SELECTED_KEY)
    if isinstance(stored, DatasetInfo):
        return stored
    try:
        return default_dataset()
    except DatasetError:
        return None


def current_dataset_id() -> str:
    """当前选中数据集的 id;一个都解析不出来时返回空串(所有会话都不可见)。"""
    info = current_dataset()
    return info.id if info else ""


# ---------------------------------------------------------------------------
# 选择驱动重建(§3.7 的坑:模块级一次性初始化 + 单例全局引擎)
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _initialize(dataset_id: str, data_dir: str, semantic_path: str) -> bool:
    """构建引擎并把它指过去。**缓存键含 dataset_id**——这是「切了也换不动」的修复点。"""
    global _active_dataset_id
    init_engine(data_dir, semantic_path)
    _active_dataset_id = dataset_id
    return True


def ensure_engine(info: DatasetInfo) -> None:
    """确保 harness.tools 的全局引擎指向 info 指向的数据集。

    st.cache_resource 命中时不会重跑函数体,而引擎是**进程级单例**:A→B→A 切回来时
    缓存命中、函数体不跑,全局引擎仍停在 B。所以判据是两条,缺一不可——
    缓存负责「不每次 rerun 都重建」,模块级账本负责「重建之后真的指对了」。
    """
    global _active_dataset_id
    _initialize(info.id, str(info.data_dir), str(info.semantic_path))
    if _active_dataset_id == info.id:
        return          # 缓存未命中(函数体刚跑过),已经指对了
    init_engine(str(info.data_dir), str(info.semantic_path))
    _active_dataset_id = info.id


# ---------------------------------------------------------------------------
# 侧边栏:选择器 + 语义层视图
# ---------------------------------------------------------------------------
def render_selector() -> DatasetInfo | None:
    """渲染侧边栏数据集选择器,并把选中的数据集同步给引擎。

    返回当前选中的数据集;一个数据集都解析不出来时返回 None(页面照常渲染,只是不能分析)。
    """
    try:
        options = available_datasets()
    except DatasetError as exc:
        st.error(f"数据集解析失败:{exc}")
        return None
    choice = _render_selectbox(options)
    if choice is None:
        return None
    st.session_state[SELECTED_KEY] = choice
    _activate(choice)
    return choice


def render_semantic_panel() -> None:
    """侧边栏「语义层」展开区:数据集元信息 + **当前选中**数据集的语义层摘要(只读)。

    渲染的是选中项而不是启动时解析的那一份——否则下拉框只是装饰(§3.7)。
    """
    info = current_dataset()
    if info is None:
        st.caption("未选中任何数据集")
        return
    try:
        with open(info.semantic_path, encoding="utf-8") as handle:
            semantic = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        st.caption(f"语义层读取失败:{exc}")
        return
    render_dataset_meta(info)
    render_semantic_view(semantic if isinstance(semantic, dict) else {})


def selection_error() -> str | None:
    """最近一次选择导致的初始化错误;None 表示当前选中的数据集可用。

    主流程据此拦下分析:引擎没指过去时继续跑,会用上一个数据集的引擎给出错误结论。
    """
    return st.session_state.get(SELECTION_ERROR_KEY)


# ---------------------------------------------------------------------------
# 会话与数据集的归属(§3.7:历史对话不该串味)
# ---------------------------------------------------------------------------
def visible_sessions(sessions) -> list:
    """当前数据集下的会话。

    按数据集**分区**而不是清空:清空会丢掉用户历史,分区则切回去还在,
    多会话历史功能(新建/切换/删除)一律不受影响,只是列表范围收窄到当前数据集。
    """
    current = current_dataset_id()
    legacy = _legacy_dataset_id()
    return [s for s in sessions if str(s.get(SESSION_DATASET_FIELD) or legacy) == current]


def migrate_legacy_sessions(sessions) -> bool:
    """给没有 dataset_id 的旧会话补上归属;返回是否有改动(调用方据此决定要不要落盘)。

    这些记录来自选择器出现之前(那时能跑的只有默认那一套数据),补一次就固定下来:
    否则归属会在每次渲染时重算,以后新增一个数据集就可能让老会话「跳到」另一套里去。
    """
    target = _legacy_dataset_id()
    changed = False
    for session in sessions:
        if not session.get(SESSION_DATASET_FIELD):
            session[SESSION_DATASET_FIELD] = target
            changed = True
    return changed


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------
def _render_selectbox(options: list[DatasetInfo]) -> DatasetInfo | None:
    """下拉控件:选项是 DatasetInfo,显示用 dataset.yaml 的 title(§3.3)。"""
    if not options:
        st.error(f"未发现任何数据集:{DATASETS_DIR} 下没有带数据清单的目录。")
        return None
    return st.selectbox(
        SELECTOR_LABEL, options, index=_initial_index(options), format_func=_display_name,
        key=SELECTOR_KEY, help=SELECTOR_HELP,
    )


def _initial_index(options: list[DatasetInfo]) -> int:
    """控件没有状态时的兜底值:先回到「当前已选中的数据集」,再退回 resolve_dataset()。

    为什么要绕这一道:selectbox 的**元素 id 由选项集合决定**(streamlit 源码
    compute_and_register_element_id 对 selectbox 只把 options/accept_new_options
    算进身份,注释写着"those can invalidate the current selection")。数据集目录一增减,
    控件在 streamlit 眼里就是一个**新控件**,旧的取值不会回填,只用 index 兜底——
    兜底值若取自磁盘上的默认数据集,用户选好的那一个就会在目录增减时漂到第一个选项,
    引擎跟着换,而界面看着却像没动过。所以兜底只能取自本模块自己的账本。
    """
    current = current_dataset()
    if current is not None:
        for index, info in enumerate(options):
            if info.id == current.id:
                return index
    return 0


def _display_name(info: DatasetInfo) -> str:
    """下拉里显示 title 而不是 id:用户选的是「零售电商归因」,不是目录名。"""
    return info.title


def _activate(info: DatasetInfo) -> None:
    """把选中项同步给引擎;失败记进 session_state,交由主流程拦下(不让页面直接崩)。"""
    try:
        ensure_engine(info)
    except Exception as exc:  # noqa: BLE001  数据集坏了可以换一个,不该炸掉整个页面
        st.session_state[SELECTION_ERROR_KEY] = f"{info.id}({info.title}):{exc}"
    else:
        st.session_state.pop(SELECTION_ERROR_KEY, None)


def _legacy_dataset_id() -> str:
    """旧会话(没有 dataset_id 字段)的归属:默认数据集。

    它们是单数据集时期写下的记录,那时能跑的只有默认那一套数据。
    """
    try:
        return default_dataset().id
    except DatasetError:
        return ""
