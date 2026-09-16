# -*- coding: utf-8 -*-
"""导入 UI:侧边栏 CSV / Excel 上传 -> **待确认**包 -> 交给确认向导落位。

装在 app/app.py 的 _render_sidebar() 里(数据集选择器之后);只做交互,真正的导入
逻辑在 harness/importer.py(纯逻辑、可单测),确认与落位在 harness/import_confirm.py。
本模块只负责:

    ① st.file_uploader 收文件
    ② 调 import_upload;识别不出时间列时让用户从列名里挑一列(非硬拒绝),再带
       date_field_override 重试;之后用 bytes 重放,不靠网络往返
    ③ 成功 -> 进入确认向导(app/import_wizard.py,渲染在主区域)—— 落位要等人工确认
    ④ 顺带:每会话清一次超时的待确认包;列出「跳过确认」落位的数据集供补确认

上传成功**不等于**多了一个数据集:包先落在 datasets/.pending/,确认(或显式跳过)
之后才进 datasets/<id>/。这是刻意的 —— 自动推断出的地图合法但可以错(见模块
harness/importer.py 的说明),不该绕过人工闸门直接出现在数据集列表里。
"""

import hashlib
import os
import sys

import streamlit as st

# 项目根与 app/ 目录都进 sys.path:本模块要么被 app.py 导入(它已加好两个),
# 要么被单独导入(测试 / 调试),两种情形都要能 import harness.* 与同目录的向导。
HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (os.path.dirname(HERE), HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from harness.import_confirm import cleanup_stale  # noqa: E402
from harness.importer import import_upload  # noqa: E402

from import_wizard import render_reconfirm_entry, start_wizard  # noqa: E402

__all__ = ["render_importer"]

# 上传控件与回显控件的稳定 key(测试 / 调试定位用)
_UPLOAD_KEY = "upload_data_file"
_UPLOAD_LABEL = "上传 CSV / Excel"
_UPLOAD_HELP = ("上传后自动按列名识别:数值列 -> 指标、低基数文本列 -> 维度,"
                "再进确认向导核对口径;确认后才成为可分析数据集(不覆盖已有数据)。")

# 无日期列回退时,用户挑时间字段的控件 key
_OVERRIDE_KEY = "upload_date_override"
_OVERRIDE_STATE_KEY = "upload_pending"   # session_state 里缓存的待重试上传字节

# 已处理过的上传指纹(见 _signature)与「本会话已清理过暂存区」的标记
_SEEN_KEY = "upload_seen_signature"
_CLEANUP_KEY = "upload_cleanup_done"


def render_importer() -> None:
    """渲染上传入口。侧边栏调用;内部只依赖 st 与本模块的 session_state。"""
    _cleanup_once()
    uploaded = st.file_uploader(_UPLOAD_LABEL, type=["csv", "xlsx", "xls"],
                                key=_UPLOAD_KEY,
                                help=_UPLOAD_HELP)
    render_reconfirm_entry()
    if uploaded is None:
        st.session_state.pop(_SEEN_KEY, None)   # 用户清掉了文件:同一份可以再传一次
        return
    signature = _signature(uploaded)
    if st.session_state.get(_SEEN_KEY) == signature:
        return            # 这是 rerun 送回来的同一份,已经处理过了
    st.session_state[_SEEN_KEY] = signature
    result = import_upload(uploaded.name, uploaded.getvalue())
    if result.get("code") == "no_date_column":
        _prompt_date_field(uploaded, columns=result.get("columns") or [])
        return
    if result.get("ok"):
        start_wizard(result["id"])
        st.rerun()
    else:
        st.error(f"导入失败:{result.get('error', '未知错误')}")


def _signature(uploaded) -> str:
    """上传文件的指纹:名字 + 内容哈希。

    为什么必须有它:st.file_uploader 在**每次 rerun 时都返回同一个文件**,而成功路径
    会 rerun —— 不记住「这份字节已经处理过」,就会一遍遍重新导入同一个文件,每轮往
    暂存区里多塞一个包(而且跳过了向导),页面再也停不下来。
    """
    data = uploaded.getvalue()
    return f"{uploaded.name}:{len(data)}:{hashlib.sha1(data).hexdigest()[:12]}"


def _prompt_date_field(uploaded, columns: list[str]) -> None:
    """没自动识别出时间列:让用户从列名里挑一列再重试(非硬拒绝)。

    字节缓存在 session_state(A 档已上传,不让用户重新拖文件)。
    """
    if not columns:   # 列名为空(同文件不应无列,但保险起见不渲染)
        return
    st.caption("未自动识别出时间列,请从下面挑一列作为时间字段")
    choice = st.selectbox("时间字段", columns, key=_OVERRIDE_KEY)
    st.session_state[_OVERRIDE_STATE_KEY] = (uploaded.name, uploaded.getvalue())
    if st.button("用这一列作为日期", key="upload_confirm_date"):
        retry = import_upload(uploaded.name, uploaded.getvalue(),
                              date_field_override=choice)
        if retry.get("ok"):
            start_wizard(retry["id"])
            st.rerun()
        else:
            st.error(f"导入失败:{retry.get('error', '未知错误')}")


def _cleanup_once() -> None:
    """每个会话清一次超时的待确认包(中断不保留:没有「待确认列表」这种 UI)。

    只跑一次而不是每帧都跑:清理要遍历暂存区,没必要每帧做。keep 传当前向导正在编辑的
    包 id —— 用户可能把向导晾了半天,按 mtime 删掉他正在改的那份是这套策略里唯一
    真正会伤到人的情形。
    """
    if st.session_state.get(_CLEANUP_KEY):
        return
    st.session_state[_CLEANUP_KEY] = True
    current = str(st.session_state.get("wizard_id") or "")
    cleanup_stale(keep=[current] if current else [])