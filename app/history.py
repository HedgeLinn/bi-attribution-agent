# -*- coding: utf-8 -*-
"""多轮对话历史(session_state)的读写与会话管理。

磁盘结构: chat_history.json = [session, ...]
  session = {"id", "title", "created_at", "messages": [msg, ...]}
  msg     = {"role": "user"/"assistant", "content", ...额外字段}

从 app.py 拆出,只为把入口文件压在 300 行约束内。HISTORY_PATH 由 app.py 计算后
经 configure() 注入,这里不再自行推导项目根,便于测试时指向临时目录。
"""

import json
import uuid
from datetime import datetime

import streamlit as st

from dataset_selector import (  # noqa: E402
    current_dataset_id,
    migrate_legacy_sessions,
    visible_sessions,
)

HISTORY_KEY = "history"          # 内存:list[dict] 会话列表
CURRENT_KEY = "current_session"  # 内存:当前会话 id
MAX_SESSIONS = 30                # 最多保留会话数,超出删最旧

HISTORY_PATH = "chat_history.json"


def _configure_history(path: str) -> None:
    """注入聊天历史文件路径(app.py 算好 ROOT 后调用)。"""
    global HISTORY_PATH
    HISTORY_PATH = path


def _migrate_flat(data):
    """旧版本是扁平 [msg, ...],迁移成单会话结构。"""
    if not data:
        return []
    if isinstance(data, list) and data and isinstance(data[0], dict) \
            and data[0].get("role") in ("user", "assistant"):
        title = next((m.get("content", "")[:30] for m in data if m.get("role") == "user"), "历史对话")
        return [{
            "id": uuid.uuid4().hex,
            "title": title,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "messages": data,
        }]
    return data


def _load_history():
    """读回会话列表(兼容旧扁平格式)。"""
    try:
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return _migrate_flat(data)
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _save_history():
    """把会话列表写回本地文件。"""
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
            json.dump(st.session_state[HISTORY_KEY], fh, ensure_ascii=False, indent=2)
    except OSError as e:
        st.warning(f"聊天记录保存失败:{e}")


def _new_session(title=""):
    """新建会话并设为当前;返回会话 dict。"""
    sess = {
        "id": uuid.uuid4().hex,
        "title": title or "新对话",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dataset_id": current_dataset_id(),   # 归属数据集:切走之后不再出现在列表里
        "messages": [],
    }
    st.session_state[HISTORY_KEY].insert(0, sess)  # 最新在最前
    st.session_state[CURRENT_KEY] = sess["id"]
    if len(st.session_state[HISTORY_KEY]) > MAX_SESSIONS:
        st.session_state[HISTORY_KEY] = st.session_state[HISTORY_KEY][:MAX_SESSIONS]
    _save_history()
    return sess


def _current_session():
    """返回当前会话 dict;没有则新建一个空的。

    只在**当前数据集**的会话里找:切换数据集后指针若落在别的数据集上,
    这里会自动落到本数据集的最近一条或新建一条,不会接着写上一个数据集的对话。
    """
    sid = st.session_state.get(CURRENT_KEY)
    sessions = visible_sessions(st.session_state[HISTORY_KEY])
    for sess in sessions:
        if sess["id"] == sid:
            return sess
    # 当前会话不存在(例如被删/迁移/换了数据集),落到本数据集最新一条
    if sessions:
        sess = sessions[0]
        st.session_state[CURRENT_KEY] = sess["id"]
        return sess
    return _new_session()


def _init_state():
    if HISTORY_KEY not in st.session_state:
        st.session_state[HISTORY_KEY] = _load_history()
        # 旧格式会话(没有 dataset_id)补一次归属并落盘:补完就固定,不再随数据集增减漂移
        if migrate_legacy_sessions(st.session_state[HISTORY_KEY]):
            _save_history()
    if CURRENT_KEY not in st.session_state:
        st.session_state[CURRENT_KEY] = None


def _append(role, payload):
    """把一条消息追加到当前会话;会话无标题时用首条用户问题作标题。"""
    sess = _current_session()
    sess["messages"].append({"role": role, **payload})
    if role == "user" and sess.get("title") in (None, "", "新对话"):
        q = (payload.get("content") or "").strip().replace("\n", " ")
        sess["title"] = q[:30] + ("…" if len(q) > 30 else "")
    _save_history()
