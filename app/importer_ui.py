# -*- coding: utf-8 -*-
"""导入 UI:侧边栏 CSV / Excel 上传 -> 自动识别指标 / 维度 -> 生成可分析数据集。

装在 app/app.py 的 _render_sidebar() 里(数据集选择器之后);只做交互,真正的导入
逻辑在 harness/importer.py(纯逻辑、可单测),这里只负责:
    ① st.file_uploader 收文件
    ② 调 import_upload;识别不出时间列时让用户从列名里挑一列(非硬拒绝),再带
       date_field_override 重试;之后用 bytes 重放,不靠网络往返
    ③ 成功 -> st.success + 展示识别出的指标 / 维度清单 + st.rerun()
       (rerun 让数据集选择器立即刷新出新数据集;上传字节缓存在 session_state,
        页面滚动条不丢)
"""

import os
import sys

import streamlit as st

# 与前缀 app.py 同一套定位方式:项目根 = 本文件所在目录的上一级
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from harness.importer import import_upload  # noqa: E402

__all__ = ["render_importer"]

# 上传控件与回显控件的稳定 key(测试 / 调试定位用)
_UPLOAD_KEY = "upload_data_file"
_UPLOAD_LABEL = "上传 CSV / Excel"
_UPLOAD_HELP = ("上传后自动按列名识别:数值列 -> 指标、低基数文本列 -> 维度,"
                "并生成独立的可分析数据集(不覆盖已有数据)。")

# 无日期列回退时,用户挑时间字段的控件 key
_OVERRIDE_KEY = "upload_date_override"
_OVERRIDE_STATE_KEY = "upload_pending"   # session_state 里缓存的待重试上传字节


def render_importer() -> None:
    """渲染上传入口。侧边栏调用;内部只依赖 st 与本模块的 session_state。"""
    uploaded = st.file_uploader(_UPLOAD_LABEL, type=["csv", "xlsx", "xls"],
                                key=_UPLOAD_KEY,
                                help=_UPLOAD_HELP)
    if uploaded is None:
        return
    result = import_upload(uploaded.name, uploaded.getvalue())
    if result.get("code") == "no_date_column":
        _prompt_date_field(uploaded, columns=result.get("columns") or [])
        return
    if result.get("ok"):
        _notify_success(result, uploaded.name)
        st.rerun()
    else:
        st.error(f"导入失败:{result.get('error', '未知错误')}")


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
            _notify_success(retry, uploaded.name)
            st.rerun()
        else:
            st.error(f"导入失败:{retry.get('error', '未知错误')}")


def _notify_success(result: dict, name: str) -> None:
    """成功回显:数据集名 + 识别出的指标与维度清单(只读)。"""
    st.success(f"已导入 {name} → 数据集 {result['id']} "
               f"({result['rows']} 行,时间列 {result['date_field']})")
    with st.expander("识别出的指标与维度", expanded=True):
        st.caption("指标(数值列):" + ("、".join(result["metrics"]) or "无"))
        st.caption("维度(低基数列):" + ("、".join(result["dimensions"]) or "无"))
