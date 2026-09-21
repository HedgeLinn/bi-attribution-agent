# -*- coding: utf-8 -*-
"""聊天记录的留存:事件编解码 + 落盘的原子性。

`app/history.py` 原先**零测试覆盖**,而这次给它加了「事件流随消息落盘」(图表因此
能随聊天记录存活)。两件事必须钉:

1. **round-trip** —— 存下去的事件读回来逐字段相等,且是紧凑单行(不被 indent=2 撑开)。
2. **失败不许毁数据** —— 旧实现是 `open("w")` **先截断**再 `json.dump`,payload 里
   混进不可序列化对象时,整份聊天历史会被清成空文件。这几条测试在旧实现下必红。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:          # app/ 内模块用平铺名互相引用
    sys.path.insert(0, str(APP_DIR))

import history  # noqa: E402


@pytest.fixture
def hist(tmp_path, monkeypatch):
    """隔离的 history:临时文件 + 替身 st(本模块只用到 warning 与 session_state)。"""
    warnings: list[str] = []
    monkeypatch.setattr(history, "st", SimpleNamespace(
        session_state={history.HISTORY_KEY: []},
        warning=lambda msg: warnings.append(str(msg))))
    monkeypatch.setattr(history, "current_dataset_id", lambda: "ds")
    monkeypatch.setattr(history, "visible_sessions", lambda sessions: sessions)
    monkeypatch.setattr(history, "migrate_legacy_sessions", lambda sessions: False)
    original = history.HISTORY_PATH
    path = tmp_path / "chat_history.json"
    history._configure_history(str(path))
    yield SimpleNamespace(warnings=warnings, path=path)
    history._configure_history(original)


def _session(messages: list) -> dict:
    return {"id": "s1", "title": "t", "created_at": "2026-01-01 00:00:00",
            "dataset_id": "ds", "messages": messages}


def test_events_round_trip() -> None:
    """events 存成**紧凑单行**串,读回来逐字段相等(含中文与浮点)。"""
    events = [{"type": "tool_call", "step": 1, "name": "contribute",
               "args": {"metric": "gmv", "dimension": "store"}},
              {"type": "tool_result", "step": 1, "name": "contribute",
               "result": {"total_base": 1.5, "label": "华东", "top": [{"key": "a"}]}}]
    encoded = history.encode_events(events)
    assert isinstance(encoded, str) and "\n" not in encoded
    assert history.decode_events({"events": encoded}) == events


def test_encode_events_filters_and_degrades() -> None:
    """usage / final 不进留存(与 token 字段、content 重复);不可序列化 -> 空数组串。"""
    encoded = history.encode_events([{"type": "usage", "input_tokens": 1},
                                     {"type": "tool_call", "step": 1, "name": "x", "args": {}}])
    assert json.loads(encoded) == [{"type": "tool_call", "step": 1, "name": "x", "args": {}}]
    assert history.encode_events([{"type": "tool_call", "args": {"obj": object()}}]) == "[]"


def test_decode_events_tolerates_old_and_dirty() -> None:
    """老消息没有 events 键 / 脏串 / 不是列表 -> 一律退化,不把回放弄崩。"""
    assert history.decode_events({}) == []
    assert history.decode_events({"events": None}) == []
    assert history.decode_events({"events": "{不是 JSON"}) == []
    assert history.decode_events({"events": '{"a": 1}'}) == []
    assert history.decode_events({"events": "[1, 2]"}) == [1, 2]
    assert history.decode_events({"events": [{"type": "tool_call"}]}) == [{"type": "tool_call"}]


def test_save_history_survives_unserializable_payload(hist) -> None:
    """不可序列化的 payload:只 warning、不抛,**磁盘上原有内容分毫不动**。"""
    history.st.session_state[history.HISTORY_KEY] = [
        _session([{"role": "assistant", "content": "原有结论"}])]
    history._save_history()
    before = hist.path.read_text(encoding="utf-8")
    assert "原有结论" in before

    history.st.session_state[history.HISTORY_KEY][0]["messages"].append(
        {"role": "assistant", "content": object()})        # 不可序列化
    history._save_history()                                # 不抛
    assert hist.warnings and "序列化" in hist.warnings[-1]
    assert hist.path.read_text(encoding="utf-8") == before  # 旧内容没被清空


def test_save_history_is_atomic_without_tmp_leftover(hist) -> None:
    """正常写入:内容可读回,且不留 .tmp 残骸。"""
    history.st.session_state[history.HISTORY_KEY] = [_session([{"role": "user", "content": "问题"}])]
    history._save_history()
    assert not (hist.path.parent / (hist.path.name + ".tmp")).exists()
    saved = json.loads(hist.path.read_text(encoding="utf-8"))
    assert saved[0]["messages"][0]["content"] == "问题"


def test_save_history_warns_when_path_unwritable(hist) -> None:
    """写盘失败(路径是个目录)-> 只 warning、不冒泡:聊天记录不许拖垮主流程。"""
    history.st.session_state[history.HISTORY_KEY] = [_session([])]
    history._configure_history(str(hist.path.parent))
    history._save_history()
    assert hist.warnings and "保存失败" in hist.warnings[-1]


def test_append_persists_events_with_message(hist) -> None:
    """`_append` 原样保存额外字段 —— 这就是 events 能随消息留存的原因。"""
    sess = history._new_session()
    history._append("assistant", {"content": "{}", "id": "m1",
                                  "events": history.encode_events(
                                      [{"type": "tool_call", "step": 1, "name": "x"}]),
                                  "parsed": None, "input_tokens": None,
                                  "output_tokens": None, "cost_cny": None})
    saved = json.loads(hist.path.read_text(encoding="utf-8"))
    msg = next(s for s in saved if s["id"] == sess["id"])["messages"][0]
    assert msg["id"] == "m1" and history.decode_events(msg)[0]["name"] == "x"