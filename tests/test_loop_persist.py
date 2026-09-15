"""harness/loop.py 结论沉淀写半侧的验收(§6.3:每次 run() 结束追加一条 annotation)。

契约:`loop.run` 的 `persist` 参数、`_to_annotation` 的键映射(「已排除」-> ruled_out、
「证据链」-> evidence,两者从 confirmed 弹出;其余键进 confirmed)。

设计约束:**不 import 真 LLM、不需要 API Key**——沉淀是纯函数 + 一次文件追加,
直接测 `_to_annotation` / `_persist_conclusion`,写入目标用 monkeypatch 指到 tmp_path。
刻意覆盖:已排除是裸字符串(模型手写 JSON 常漏方括号)、非 JSON 不写、
persist=False 不写、写入失败不抛。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from attribution.annotations import AnnotationError, load_annotations
from harness import loop

_TS = "2031-01-02T03:04:05+08:00"

# 按提示词输出格式给出的「标准」结论:全部键齐
FULL = ('{"结论": "华东区域下滑", "根因": {"dimension": "store", "key": "S1"}, '
        '"量级": {"贡献度": 0.9}, "已排除": ["促销", "季节"], '
        '"证据链": ["detect_anomaly 判异常", "contribute 定位到门店"], '
        '"无法归因": false, "置信度": "high", "建议": "盯住该店"}')

# 模型只给了部分键(已排除写成裸字符串——parser 之外的真实形态)
PARTIAL = ('{"结论": "整体自然回落", "已排除": "大促影响", "无法归因": true}')

PROSE = "整体是自然回落,但某一家表现明显异常。"


def _patch_target(monkeypatch, tmp_path, dataset: str = "demo") -> str:
    """把沉淀目标指到 tmp_path,返回目标文件路径(不真实触碰 datasets/)。"""
    monkeypatch.setattr(loop, "DEFAULT_DATASETS_DIRNAME", str(tmp_path))
    monkeypatch.setattr(loop.tools, "semantic",
                        lambda: SimpleNamespace(dataset=dataset))
    return str(tmp_path / dataset / "annotations.jsonl")


# --- 1. 键映射:ruled_out / evidence 弹出,其余进 confirmed ---

def test_to_annotation_splits_ruled_out_and_evidence():
    annotation = loop._to_annotation("为什么下滑?", FULL, _TS)

    assert annotation.ts == _TS
    assert annotation.query == "为什么下滑?"
    assert annotation.ruled_out == ("促销", "季节")
    assert annotation.evidence == ("detect_anomaly 判异常", "contribute 定位到门店")
    assert "已排除" not in annotation.confirmed
    assert "证据链" not in annotation.confirmed
    assert annotation.confirmed["结论"] == "华东区域下滑"   # 其余键原样保留


def test_to_annotation_wraps_bare_string_lists():
    annotation = loop._to_annotation("为什么下滑?", PARTIAL, _TS)

    assert annotation.ruled_out == ("大促影响",)           # 裸字符串 -> 单元素
    assert annotation.evidence == ()                       # 缺键 -> 空
    assert annotation.confirmed["无法归因"] is True


def test_to_annotation_rejects_unparseable_content():
    assert loop._to_annotation("为什么下滑?", PROSE, _TS) is None
    assert loop._to_annotation("为什么下滑?", "", _TS) is None


def test_to_annotation_ignores_non_list_ruled_out():
    """已排除写成了数字之类:静默丢弃(与 annotations 读侧「类型不符必抛」不同——
    写侧宁缺毋滥,不把垃圾写进沉淀)。"""
    annotation = loop._to_annotation(
        "为什么下滑?", '{"结论": "x", "已排除": 123}', _TS)
    assert annotation.ruled_out == ()
    assert annotation.confirmed["结论"] == "x"


# --- 2. 写文件:追加、可回读、开关生效 ---

def test_persist_writes_and_appends(tmp_path, monkeypatch):
    target = _patch_target(monkeypatch, tmp_path)

    loop._persist_conclusion("为什么下滑?", FULL, persist=True, verbose=False)
    loop._persist_conclusion("为什么下滑?", PARTIAL, persist=True, verbose=False)

    saved = load_annotations(target)
    assert len(saved) == 2                                   # 追加,不覆盖
    assert saved[0].query == saved[1].query == "为什么下滑?"
    assert saved[0].ruled_out == ("促销", "季节")
    assert saved[1].ruled_out == ("大促影响",)
    assert saved[1].confirmed["结论"] == "整体自然回落"


def test_persist_silently_skips_unparseable(tmp_path, monkeypatch):
    target = _patch_target(monkeypatch, tmp_path)

    loop._persist_conclusion("为什么下滑?", PROSE, persist=True, verbose=True)

    assert load_annotations(target) == []                    # 非 JSON 不落盘


def test_persist_false_writes_nothing(tmp_path, monkeypatch):
    target = _patch_target(monkeypatch, tmp_path)

    loop._persist_conclusion("为什么下滑?", FULL, persist=False, verbose=False)

    assert load_annotations(target) == []


def test_persist_failure_does_not_raise(tmp_path, monkeypatch):
    """沉淀失败(如语义层不可用)不许拖垮主流程:静默放弃。"""
    target = _patch_target(monkeypatch, tmp_path)
    monkeypatch.setattr(loop, "append_annotation",
                        lambda path, annotation: (_ for _ in ()).throw(AnnotationError("坏")))

    loop._persist_conclusion("为什么下滑?", FULL, persist=True, verbose=False)

    assert load_annotations(target) == []


def test_persist_when_semantic_unavailable_does_not_raise(tmp_path, monkeypatch):
    """语义层单例未初始化(run 的假件环境):target 拼不出 -> 静默跳过。"""
    monkeypatch.setattr(loop, "DEFAULT_DATASETS_DIRNAME", str(tmp_path))
    monkeypatch.setattr(loop.tools, "semantic", lambda: None)

    loop._persist_conclusion("为什么下滑?", FULL, persist=True, verbose=False)


# --- 3. run() 接线:两处出口都触发沉淀(不改 run,只观察文件) ---

def _patch_run(monkeypatch, llm):
    monkeypatch.setattr(loop, "build_llm", lambda tools=None: llm)
    monkeypatch.setattr(loop.tools, "build_tools", lambda: [])
    monkeypatch.setattr(loop.tools, "semantic", lambda: SimpleNamespace(dataset="demo"))
    monkeypatch.setattr(loop.tools, "dataset_context", lambda: {})
    monkeypatch.setattr(loop.context, "render_system_prompt", lambda *a, **k: "sys")


def test_run_exit_persists_conclusion(tmp_path, monkeypatch):
    """正常结束路径:run() 返回后,结论已在数据集目录里(TS 由函数生成,不断言具体值)。"""
    from langchain_core.messages import AIMessage

    monkeypatch.setattr(loop, "DEFAULT_DATASETS_DIRNAME", str(tmp_path))
    _patch_run(monkeypatch, SimpleNamespace(invoke=lambda messages: AIMessage(content=FULL)))
    monkeypatch.setattr(loop, "_emit_finish", lambda *a, **k: None)  # 隔离成本显示

    loop.run("为什么下滑?", verbose=False)

    saved = load_annotations(str(tmp_path / "demo" / "annotations.jsonl"))
    assert len(saved) == 1
    assert saved[0].query == "为什么下滑?"
    assert saved[0].ruled_out == ("促销", "季节")


def test_run_with_persist_false_writes_nothing(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage

    monkeypatch.setattr(loop, "DEFAULT_DATASETS_DIRNAME", str(tmp_path))
    _patch_run(monkeypatch, SimpleNamespace(invoke=lambda messages: AIMessage(content=FULL)))
    monkeypatch.setattr(loop, "_emit_finish", lambda *a, **k: None)

    loop.run("为什么下滑?", verbose=False, persist=False)

    assert load_annotations(str(tmp_path / "demo" / "annotations.jsonl")) == []