# -*- coding: utf-8 -*-
"""多轮对话历史(session_state)的读写与会话管理。

磁盘结构: chat_history.json = [session, ...]
  session = {"id", "title", "created_at", "messages": [msg, ...]}
  msg     = {"role": "user"/"assistant", "content", ...额外字段}

从 app.py 拆出,只为把入口文件压在 300 行约束内。HISTORY_PATH 由 app.py 计算后
经 configure() 注入,这里不再自行推导项目根,便于测试时指向临时目录。
"""

import json
import os
import uuid
from datetime import datetime

import streamlit as st

from chart_data import keep_events       # 事件裁剪(纯数据层,不依赖 streamlit)
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


def encode_events(events):
    """事件流 -> **紧凑 JSON 字符串**(消息 payload 里就存这个)。

    存成字符串而不是 list,是因为落盘用的是 `indent=2`:嵌套的事件流被展开后体积会翻几倍。
    序列化失败返回 `"[]"` —— 画不出图是可以接受的降级,**存不下去才是灾难**。
    """
    try:
        return json.dumps(keep_events(events), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "[]"


def decode_events(msg):
    """消息里的 `events` -> 列表。

    兼容三种形态:紧凑字符串(现格式)/ 直接存的 list / 老消息根本没有这个键。
    任何解析失败都退化成空列表 —— 回放画不出图,但不许把回放本身弄崩。
    """
    raw = msg.get("events")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _save_history():
    """把会话列表**原子**写回本地文件。

    两个防线(旧实现两条都没有):

    1. **先序列化到内存再落盘**。旧写法是 `open("w")` 先截断再 `json.dump`,而 `json.dump`
       中途抛 `TypeError`(事件里混进不可序列化对象)时,磁盘上**整份聊天历史已被清空**。
    2. **写盘失败不冒泡**。聊天记录是锦上添花,不许拖垮主流程。
    """
    try:
        payload = json.dumps(st.session_state[HISTORY_KEY], ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as e:
        st.warning(f"聊天记录保存失败(内容无法序列化),本次未写入:{e}")
        return
    # 临时名必须唯一:chat_history.json 是所有浏览器会话共享的一个文件,写死的 `.tmp`
    # 会让并发写交叠进同一份文件(两个标签页就能触发)。
    tmp = f"{HISTORY_PATH}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, HISTORY_PATH)       # 原子替换:读到的一半文件不存在
    except OSError as e:
        st.warning(f"聊天记录保存失败:{e}")
        try:
            os.unlink(tmp)                  # 别留下含聊天记录明文的残骸
        except OSError:
            pass


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
