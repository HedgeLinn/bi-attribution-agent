"""归因结论沉淀与选择性回注的验收(M6,§6.3):存得住、选得准、错得起。

三层各测各的:
    1. 存储(attribution/annotations.py)——坏行必抛(沉淀是评估的输入,静默丢行等于
       悄悄改分母)、原子追加(写一半崩掉不留半行)、目录自建
    2. 选择——词面重叠排序 / 无重叠返回空 / 同分按 ts 升序 / top_n 截断 / 中文分词
    3. 回注(harness/context.py)——没给问题就与改造前逐字相同;给了问题且选得出相关项
       才注入;沉淀文件缺失或有坏行都只安静跳过,不许拖垮主流程

夹具不依赖任何真实数据集:内联语义层 + tmp_path(与真实数据集词汇刻意不重合)。
"""

from __future__ import annotations

import json
import os

import pytest

from attribution.annotations import (Annotation, AnnotationError, append_annotation,
                                     load_annotations, select_relevant)
from attribution.semantic import Semantic
from harness import context

# tests/ 是包(有 __init__.py),共享夹具走包内导入
from tests.semantic_fixtures import BASE_YAML, write_layer

DATASET = "unit-test-shop"          # BASE_YAML 里的 dataset 标识


def _ann(ts: str = "2026-09-01T10:00:00", query: str = "问题", **fields) -> Annotation:
    """构造一条沉淀;只关心个别字段的用例从简。"""
    return Annotation(ts=ts, query=query, **fields)


def _write(path, *annotations: Annotation) -> str:
    """把若干条沉淀写进 path,返回路径字符串。"""
    for one in annotations:
        append_annotation(str(path), one)
    return str(path)


# --- 存储:读 -----------------------------------------------------------------
def test_load_missing_file_returns_empty(tmp_path) -> None:
    """没有沉淀文件不是错误:「还没沉淀过」是正常状态。"""
    assert load_annotations(str(tmp_path / "annotations.jsonl")) == []


def test_load_reads_fields_in_file_order(tmp_path) -> None:
    """正常文件:字段逐个还原,token 化要用的三块文本都在。"""
    path = _write(tmp_path / "annotations.jsonl",
                  _ann(ts="2026-08-01T09:00:00", query="为什么 7 月毛利下滑",
                       hypotheses=("价降",), confirmed={"结论": "折扣加深"},
                       ruled_out=("成本上升",), evidence=("contribute: 折扣率 +3pt",)),
                  _ann(ts="2026-09-01T09:00:00", query="为什么 8 月复购下降"))
    loaded = load_annotations(path)
    assert [one.ts for one in loaded] == ["2026-08-01T09:00:00", "2026-09-01T09:00:00"]
    assert loaded[0].hypotheses == ("价降",) and loaded[0].confirmed == {"结论": "折扣加深"}
    assert loaded[0].ruled_out == ("成本上升",)
    assert loaded[0].evidence == ("contribute: 折扣率 +3pt",)
    assert loaded[1].hypotheses == () and loaded[1].confirmed == {}, "缺省字段必须是空而不是 None"


def test_load_tolerates_hand_edits(tmp_path) -> None:
    """手改的文件要收:空行不是记录,列表字段写成单个字符串也算一条(漏方括号很常见)。"""
    path = tmp_path / "annotations.jsonl"
    path.write_text('\n{"ts": "t1", "query": "q1", "ruled_out": "只有一条"}\n\n', encoding="utf-8")
    loaded = load_annotations(str(path))
    assert [one.ts for one in loaded] == ["t1"] and loaded[0].ruled_out == ("只有一条",)


@pytest.mark.parametrize("bad", ["{不是 JSON", "[1, 2]", '"一段文本"', "12"])
def test_load_raises_on_broken_line(tmp_path, bad) -> None:
    """坏行必抛:JSON 非法、不是对象都算坏,而且错误信息必须带行号(否则等于没说)。"""
    path = tmp_path / "annotations.jsonl"
    path.write_text('{"ts": "t1", "query": "q1"}\n' + bad + "\n", encoding="utf-8")
    with pytest.raises(AnnotationError) as err:
        load_annotations(str(path))
    assert ":2:" in str(err.value), "错误信息必须点名文件与行号"


@pytest.mark.parametrize("missing", ["ts", "query"])
def test_load_raises_on_missing_required_key(tmp_path, missing) -> None:
    """必填键 ts / query 缺失或空白:这条沉淀排不了序、选不了相关性,读进来就是坏的。"""
    path = tmp_path / "annotations.jsonl"
    path.write_text(json.dumps({"ts": "t1", "query": "q1", missing: ""}), encoding="utf-8")
    with pytest.raises(AnnotationError):
        load_annotations(str(path))


@pytest.mark.parametrize("bad_field", [
    '{"ts": "t1", "query": "q1", "hypotheses": {"a": 1}}',
    '{"ts": "t1", "query": "q1", "ruled_out": [1, 2]}',
    '{"ts": "t1", "query": "q1", "confirmed": ["a"]}'])
def test_load_raises_on_wrong_field_type(tmp_path, bad_field) -> None:
    """类型不符也抛:静默改写成别的形状,才是真正危险的宽容。"""
    path = tmp_path / "annotations.jsonl"
    path.write_text(bad_field + "\n", encoding="utf-8")
    with pytest.raises(AnnotationError):
        load_annotations(str(path))


# --- 存储:写(原子追加) --------------------------------------------------------
def test_append_creates_directory_and_file(tmp_path) -> None:
    """目录不存在时自建;写进去的能原样读回来(读写同口径),且不留临时文件。"""
    path = tmp_path / "datasets" / DATASET / "annotations.jsonl"
    append_annotation(str(path), _ann(query="为什么 6 月华东 GMV 下滑"))
    assert path.is_file() and list(tmp_path.rglob("*.tmp")) == []
    assert load_annotations(str(path))[0].query == "为什么 6 月华东 GMV 下滑"


def test_append_is_additive_and_keeps_order(tmp_path) -> None:
    """追加不覆盖:顺序等于写入顺序(评估按 ts 读历史,顺序错了会串味)。"""
    path = _write(tmp_path / "annotations.jsonl",
                  _ann(ts="2026-08-01", query="第一条"),
                  _ann(ts="2026-09-01", query="第二条"))
    append_annotation(path, _ann(ts="2026-10-01", query="第三条"))
    assert [one.query for one in load_annotations(path)] == ["第一条", "第二条", "第三条"]


def test_append_repairs_missing_trailing_newline(tmp_path) -> None:
    """手改过的文件末尾没有换行时,新记录另起一行,不许与旧行粘成一行。"""
    path = tmp_path / "annotations.jsonl"
    path.write_text('{"ts": "t1", "query": "旧的"}', encoding="utf-8")
    append_annotation(str(path), _ann(ts="t2", query="新的"))
    assert len(load_annotations(str(path))) == 2


def test_append_keeps_record_on_one_line(tmp_path) -> None:
    """带换行的多行结论也必须压成一行:JSONL 一行一条是硬约束。"""
    path = tmp_path / "annotations.jsonl"
    append_annotation(str(path), _ann(query="第一行\n第二行"))
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 1
    assert load_annotations(str(path))[0].query == "第一行\n第二行"


def test_append_keeps_old_content_when_replace_fails(tmp_path, monkeypatch) -> None:
    """原子性:替换失败时旧文件必须原样可用,且不留下半截临时文件。"""
    path = _write(tmp_path / "annotations.jsonl", _ann(ts="t1", query="已存在"))

    def boom(*args, **kwargs):
        raise OSError("磁盘写满")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        append_annotation(path, _ann(ts="t2", query="写不进去"))
    assert [one.query for one in load_annotations(path)] == ["已存在"]
    assert list(tmp_path.rglob("*.tmp")) == []


@pytest.mark.parametrize("field", ["ts", "query"])
def test_append_rejects_blank_required_field(tmp_path, field) -> None:
    """先校验再落盘:写出一条自己都读不回来的记录,等于给下次加载埋雷。"""
    path = tmp_path / "annotations.jsonl"
    with pytest.raises(AnnotationError):
        append_annotation(str(path), _ann(**{field: "  "}))
    assert not path.exists(), "校验失败时不该创建文件"


# --- 选择:词面重叠 -----------------------------------------------------------
def test_select_ranks_by_shared_token_count() -> None:
    """命中多的排前面:这是「选择性注入」的全部排序依据。"""
    near = _ann(ts="t1", query="为什么 6 月华东 GMV 下滑", confirmed={"结论": "门店断崖"})
    far = _ann(ts="t2", query="华东地区整体表现", confirmed={"结论": "无异常"})
    other = _ann(ts="t3", query="为什么复购率下降", confirmed={"结论": "会员流失"})
    picked = select_relevant([other, far, near], "为什么 6 月华东 GMV 下滑")
    assert [one.ts for one in picked] == ["t1", "t2"], "按重叠度降序,无关的不进结果"


def test_select_returns_empty_without_overlap() -> None:
    """宁缺毋滥:一条都不相关时返回空,而不是硬凑 top_n 条。"""
    assert select_relevant([_ann(query="为什么复购率下降"),
                            _ann(query="为什么客单价走低")], "为什么 6 月华东 GMV 下滑") == []


def test_select_breaks_ties_by_ts_ascending() -> None:
    """同分按 ts 升序:同输入必得同输出,免得提示词随文件顺序抖动。"""
    first = _ann(ts="2026-08-01", query="华东 GMV 下滑")
    second = _ann(ts="2026-09-01", query="华东 GMV 下滑")
    assert [one.ts for one in select_relevant([second, first], "华东 GMV 下滑")] == [
        "2026-08-01", "2026-09-01"]


def test_select_truncates_to_top_n() -> None:
    """top_n 截断:注入条数有上界,历史不该挤掉本次分析的空间。"""
    rows = [_ann(ts=f"t{i}", query="华东 GMV 下滑") for i in range(5)]
    assert len(select_relevant(rows, "华东 GMV 下滑", top_n=2)) == 2
    assert len(select_relevant(rows, "华东 GMV 下滑")) == 3, "默认 top_n=3"


@pytest.mark.parametrize("top_n", [0, -1])
def test_select_non_positive_top_n_returns_empty(top_n) -> None:
    """top_n ≤ 0 = 不注入任何历史(不是「全部注入」)。"""
    assert select_relevant([_ann(query="华东 GMV 下滑")], "华东 GMV 下滑", top_n) == []


def test_select_looks_at_hypotheses_and_confirmed() -> None:
    """相关性不只看原问题:假设与被证实的结论同样参与(§6.3 定的口径)。"""
    by_hypothesis = _ann(ts="t1", query="常规复盘", hypotheses=("华东门店客流下滑",))
    by_conclusion = _ann(ts="t2", query="常规复盘", confirmed={"根因": "头部商品下架"})
    picked = select_relevant([by_hypothesis, by_conclusion], "华东门店为什么客流下滑")
    assert [one.ts for one in picked] == ["t1"]
    assert select_relevant([by_hypothesis, by_conclusion], "头部商品下架的影响")[0].ts == "t2"


def test_select_ignores_function_words() -> None:
    """虚词不算重叠:两条问题只共享「为什么/的」这类问句骨架时不是相关性(英文同理)。"""
    assert select_relevant([_ann(query="why did it happen")], "how was this done") == []
    assert select_relevant([_ann(query="为什么毛利率")], "为什么复购率") == []


def test_select_chinese_unit_word_alone_is_not_relevance() -> None:
    """「6 月」与「7 月」只共享一个「月」:单字重叠撑不起相关性,必须区分开。"""
    july = _ann(ts="t1", query="为什么 7 月销售额上升")
    june = _ann(ts="t2", query="为什么 6 月华东 GMV 下滑")
    assert [one.ts for one in select_relevant([july, june], "为什么 6 月华东 GMV 下滑")] == ["t2"]


def test_select_matches_chinese_beyond_exact_phrase() -> None:
    """中文靠二字词对上:措辞不同但词相同的结论仍要选得出来。"""
    row = _ann(query="华东地区 GMV 下滑的根因分析", confirmed={"结论": "门店断崖下跌"})
    assert select_relevant([row], "为什么 6 月华东 GMV 下滑") == [row]


def test_select_is_case_insensitive_and_deterministic() -> None:
    """大小写与词序无关(ASCII 词统一小写);同一输入必得同一输出(提示词可复现)。"""
    row = _ann(query="GMV decline in East China")
    assert select_relevant([row], "why did gmv decline") == [row]
    rows = [_ann(ts=f"t{i}", query="华东 GMV 下滑") for i in range(4)]
    assert select_relevant(rows, "华东 GMV 下滑") == select_relevant(rows, "华东 GMV 下滑")


@pytest.mark.parametrize("query", ["", "   ", "为什么会"])
def test_select_empty_query_returns_empty(query) -> None:
    """问题本身没有实词时选不出相关项——不注入,而不是把历史全塞进去。"""
    assert select_relevant([_ann(query="华东 GMV 下滑")], query) == []


# --- 回注:提示词里的「历史归因结论」段 -----------------------------------------
@pytest.fixture
def layer(tmp_path) -> Semantic:
    """内联语义层(不依赖任何真实数据集文件)。"""
    return Semantic.load(write_layer(tmp_path, BASE_YAML))


def _seed(tmp_path, *annotations: Annotation) -> None:
    """按数据集布局落下沉淀文件:datasets/<id>/annotations.jsonl。"""
    path = tmp_path / "datasets" / DATASET / "annotations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    for one in annotations:
        append_annotation(str(path), one)


def test_prompt_without_query_is_byte_identical(layer) -> None:
    """不给 query 时输出与改造前**逐字相同**(M6 之前的那一行):老调用方零影响。"""
    before_m6 = f"{context._METHODOLOGY}\n{context.render_dataset_context(layer)}"
    assert context.render_system_prompt(layer) == before_m6
    assert context.render_system_prompt(layer, context.DatasetContext()) == before_m6
    assert context.render_system_prompt(layer, None, query=None) == before_m6


def test_prompt_injects_only_relevant_history(layer, tmp_path, monkeypatch) -> None:
    """给了问题才注入,且只注入相关的那些(段落里能看到时间/原问题/要点/已排除)。"""
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path,
          _ann(ts="2026-09-13T10:00:00", query="为什么 6 月华东 GMV 下滑?",
               confirmed={"结论": "上海旗舰门店断崖下跌"}, ruled_out=("618 大促后回落",)),
          _ann(ts="2026-08-01T10:00:00", query="为什么复购率下降", confirmed={"结论": "会员流失"}))
    prompt = context.render_system_prompt(layer, context.DatasetContext(), "为什么 6 月华东 GMV 下滑")
    assert "## 历史归因结论" in prompt
    assert "2026-09-13T10:00:00" in prompt and "上海旗舰门店断崖下跌" in prompt
    assert "已排除: 618 大促后回落" in prompt
    assert "会员流失" not in prompt, "不相关的历史结论不该出现在提示词里"
    unrelated = context.render_system_prompt(layer, context.DatasetContext(), "为什么 9 月新增订阅放缓")
    assert "## 历史归因结论" not in unrelated, "无关的问题连段落都不该有"


def test_prompt_skips_history_when_absent_or_broken(layer, tmp_path, monkeypatch) -> None:
    """沉淀不存在或坏了都安静跳过:没有段落,但提示词其余部分完好——不许拖垮主流程。"""
    monkeypatch.chdir(tmp_path)
    missing = context.render_system_prompt(layer, context.DatasetContext(), "为什么华东 GMV 下滑")
    assert "## 历史归因结论" not in missing
    assert missing.endswith(context.render_dataset_context(layer, context.DatasetContext()))
    path = tmp_path / "datasets" / DATASET / "annotations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{这行是坏的\n", encoding="utf-8")
    broken = context.render_system_prompt(layer, context.DatasetContext(), "为什么华东 GMV 下滑")
    assert "## 历史归因结论" not in broken and "可用指标" in broken
