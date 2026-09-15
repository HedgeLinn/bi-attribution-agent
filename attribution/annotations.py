"""语义收集 C 档(§6.3):归因结论沉淀为 annotations,并按相关性选择性回注。

纯逻辑模块:**不碰 LLM、不碰 DuckDB、不渲染提示词**——回注时把选中的
annotations 交给 harness/context 渲染,本模块只负责「存」与「选」。

存储:`datasets/<name>/annotations.jsonl`,一行一条:
    {"ts": "...", "query": "...", "hypotheses": [...], "confirmed": {...},
     "ruled_out": [...], "evidence": [...]}
相关性 = 词面重叠(共享分词后的 token 数),取 top-n——不引向量模型,
「选择性注入」的目的是避免把整本历史塞进提示词,不是做语义检索。

受 test_engine_has_no_hardcoded_vocabulary 的 AST 扫描约束:可执行代码里
不得出现数据集词汇,docstring 与注释里的举例除外。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Annotation", "append_annotation", "load_annotations",
           "select_relevant"]

# 分词:ASCII 词(字母/数字/下划线连续段)整体成一个 token;CJK 连续段交给 _cjk_tokens。
# 按「字符类别」而不是空白切:中文不靠空格分词,「为什么6月华东GMV下滑」与
# 「为什么 6 月华东 GMV 下滑」必须切出同一组 token,否则词面匹配会退化成
# 「整句一字不差才算相关」。
_TOKEN_RE = re.compile(r"[0-9a-z_]+|[一-鿿]+")

# 英文停用词只收最通用的虚词:它们不携带业务信息,留着只会让任意两条结论都"恰好相关"。
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "did", "do",
    "does", "for", "from", "had", "has", "have", "how", "in", "is", "it",
    "its", "not", "of", "on", "or", "that", "the", "then", "this", "to",
    "was", "were", "what", "when", "where", "which", "who", "why", "with",
})

# 中文停用字:问句骨架(为什么/吗呢)与量词单位(年月号)。**在切二字之前逐字去掉**,
# 这样「6 月华东」只剩「华东」——否则「月华」这个跨词的二字组会把华东与华南混为一谈。
_CJK_STOPCHARS: frozenset[str] = frozenset(
    "的了吗呢吧啊么什为怎哪我你他她它们个这那和与是在及而或于之其把被从对以会"
    "给让到着过都也还很就些月年日号季度")


class AnnotationError(ValueError):
    """annotations.jsonl 的读写失败:坏行、必填键缺失、字段类型不符、文件不可读写。

    继承 ValueError:只关心「值不对」的调用方不必认识这个类型也能接住它。
    """


@dataclass(frozen=True)
class Annotation:
    """一条沉淀的归因结论(对应 annotations.jsonl 的一行)。"""

    ts: str
    query: str
    hypotheses: tuple[str, ...] = ()
    confirmed: dict[str, Any] = field(default_factory=dict)
    ruled_out: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


def load_annotations(path: str) -> list[Annotation]:
    """读 annotations.jsonl;文件不存在返回空列表;单行损坏跳过并记录?——
    不,坏行抛错:沉淀是评估的输入,静默丢行等于悄悄改分母。"""
    source = Path(path)
    if not source.is_file():
        return []
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as err:
        raise AnnotationError(f"读取失败:{source}:{err}") from err
    return [_parse_line(line, source, number)
            for number, line in enumerate(text.splitlines(), start=1)
            if line.strip()]   # 空行不是一条记录(手改文件留下的空行不该算坏行)


def append_annotation(path: str, annotation: Annotation) -> None:
    """追加一条到 annotations.jsonl(原子写:同目录临时文件 + rename);
    目录不存在时创建。"""
    _check_fields(annotation)          # 写进去的必须读得回来,否则就是给下次加载埋雷
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(_payload(annotation), ensure_ascii=False) + "\n"
    _atomic_write(target, _existing_text(target) + line)


def select_relevant(annotations: list[Annotation], query: str, top_n: int = 3
                    ) -> list[Annotation]:
    """按与 query 的词面重叠度取 top_n 条;无重叠时返回空(宁缺毋滥)。

    分词:ASCII 段整体成词;CJK 逐字去停用字后取**相邻二字**成词(单字共享会让
    「月」这类字把无关问题误注入,二字词切分能挡住)。重叠度 = query token 与
    annotation.query/hypotheses/confirmed 文本共享 token 数。
    排序稳定(同分按 ts 升序),保证「同输入 -> 同输出」。
    """
    if top_n <= 0:
        return []
    wanted = _tokens(query)
    if not wanted:                     # 问题里没有实词(如只有「为什么」):选不出相关项
        return []
    scored = [(_overlap(wanted, item), item) for item in annotations]
    ranked = sorted((hit for hit in scored if hit[0] > 0),
                    key=lambda hit: (-hit[0], hit[1].ts))
    return [item for _, item in ranked[:top_n]]


# ---------------------------------------------------------------------------
# 内部:一行 JSON 的读法(坏行必抛)与写法(原子追加)
# ---------------------------------------------------------------------------
def _parse_line(line: str, source: Path, number: int) -> Annotation:
    """一行 -> Annotation;JSON 非法、不是对象、必填键缺失或空白都抛错并带上行号。

    错误信息必须带 (文件, 行号):沉淀是人工也会手改的文件,只说「有坏行」等于没说。
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as err:
        raise AnnotationError(f"{source}:{number}: 不是合法 JSON:{err.msg}") from err
    if not isinstance(raw, Mapping):
        raise AnnotationError(
            f"{source}:{number}: 每行必须是一个 JSON 对象,实际是 {type(raw).__name__}")
    return Annotation(
        ts=_required_text(raw, "ts", source, number),
        query=_required_text(raw, "query", source, number),
        hypotheses=_text_tuple(raw.get("hypotheses"), "hypotheses", source, number),
        confirmed=_object(raw.get("confirmed"), source, number),
        ruled_out=_text_tuple(raw.get("ruled_out"), "ruled_out", source, number),
        evidence=_text_tuple(raw.get("evidence"), "evidence", source, number),
    )


def _required_text(raw: Mapping, key: str, source: Path, number: int) -> str:
    """必填文本键:缺失、非文本或全是空白都抛错(空 ts 排不了序,空 query 选不出相关性)。"""
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AnnotationError(f"{source}:{number}: 缺少必填字段 {key}(非空文本)")
    return value


def _text_tuple(value: Any, key: str, source: Path, number: int) -> tuple[str, ...]:
    """可选文本列表:缺失 -> 空;单个字符串按一条收(手写时常漏方括号);
    类型不符则抛错——静默改写成别的形状才是真正危险的宽容。"""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        if not all(isinstance(item, str) for item in value):
            raise AnnotationError(f"{source}:{number}: {key} 必须是文本列表")
        return tuple(item for item in value if item.strip())
    raise AnnotationError(
        f"{source}:{number}: {key} 必须是文本或文本列表,实际是 {type(value).__name__}")


def _object(value: Any, source: Path, number: int) -> dict[str, Any]:
    """可选对象键(confirmed):缺失 -> 空字典;不是对象则抛错。"""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    raise AnnotationError(f"{source}:{number}: confirmed 必须是对象,实际是 {type(value).__name__}")


def _payload(annotation: Annotation) -> dict[str, Any]:
    """Annotation -> 一行 JSON 的字段(键名与读入侧对称,顺序固定便于人工比对)。"""
    return {"ts": annotation.ts, "query": annotation.query,
            "hypotheses": list(annotation.hypotheses),
            "confirmed": dict(annotation.confirmed),
            "ruled_out": list(annotation.ruled_out),
            "evidence": list(annotation.evidence)}


def _check_fields(annotation: Annotation) -> None:
    """写入前校验:与读入同一套必填/类型规则,防止写出自己都读不回来的行。"""
    for key, value in (("ts", annotation.ts), ("query", annotation.query)):
        if not isinstance(value, str) or not value.strip():
            raise AnnotationError(f"{key} 不能为空(写进去的必须读得回来)")
    for key, values in (("hypotheses", annotation.hypotheses),
                        ("ruled_out", annotation.ruled_out),
                        ("evidence", annotation.evidence)):
        if not all(isinstance(item, str) for item in values):
            raise AnnotationError(f"{key} 必须是文本列表")
    if not isinstance(annotation.confirmed, Mapping):
        raise AnnotationError("confirmed 必须是对象")


def _existing_text(target: Path) -> str:
    """已有内容(文件不存在视为空);末尾缺换行时补一个,免得新旧两行粘成一行。"""
    if not target.is_file():
        return ""
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as err:
        raise AnnotationError(f"读取失败:{target}:{err}") from err
    return text + "\n" if text and not text.endswith("\n") else text


def _atomic_write(target: Path, text: str) -> None:
    """同目录临时文件 + os.replace:崩在写一半时目标文件仍是完整的老版本。

    必须同目录:跨盘 rename 不是原子操作。fsync 后才 replace,断电也不会replace到空文件。
    """
    tmp = tempfile.NamedTemporaryFile(dir=target.parent, prefix=target.name + ".",
                                      mode="w", encoding="utf-8", newline="\n",
                                      delete=False)
    try:
        with tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, target)
    except BaseException:              # 失败也要清掉临时文件,不给目录留垃圾
        Path(tmp.name).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# 内部:相关性 = 共享 token 数
# ---------------------------------------------------------------------------
def _tokens(text: str) -> set[str]:
    """文本 -> token 集合:ASCII 段整体成词、CJK 段按二字切,再统一去停用词。

    用集合而非列表:「共享 token 数」不该因为同一个词重复出现而加分;
    集合也让结果与词序无关——同输入必得同输出。
    """
    found: set[str] = set()
    for piece in _TOKEN_RE.findall(text.lower()):
        found.update({piece} if piece[0].isascii() else _cjk_tokens(piece))
    return found - _STOPWORDS


def _cjk_tokens(run: str) -> set[str]:
    """CJK 连续段 -> token 集合:先逐字去停用字,剩下的按**相邻二字**切。

    为什么不按单字切:「6 月」与「7 月」会共享「月」,于是任意两条同月份的结论
    都算「相关」——单字重叠太弱,撑不起「选择性注入」。二字词才是有信息量的单位,
    而「华东地区」与「华东」靠共享的二字词仍能对上(单字切分反而对不上)。
    段长不足两字时用它自己;去停用字后为空则该段不贡献 token。
    """
    kept = "".join(char for char in run if char not in _CJK_STOPCHARS)
    if len(kept) < 2:
        return {kept} if kept else set()
    return {kept[index:index + 2] for index in range(len(kept) - 1)}


def _searchable(annotation: Annotation) -> str:
    """一条沉淀里参与重叠度计算的文本:query + hypotheses + confirmed(§6.3 的口径)。

    ruled_out / evidence 刻意不参与:被否掉的假设与新问题共享词面,恰恰说明新问题
    可能也是错的假设;证据链是长文本,混进来会让重叠度被细节噪声主导。
    """
    return "\n".join([annotation.query, *annotation.hypotheses,
                      *_flatten(annotation.confirmed)])


def _flatten(value: Any) -> list[str]:
    """把 confirmed 这类嵌套结构摊平成文本片段:键名也收(键里常带业务词)。"""
    if isinstance(value, Mapping):
        return [piece for key, item in value.items()
                for piece in (str(key), *_flatten(item))]
    if isinstance(value, (list, tuple, set)):
        return [piece for item in value for piece in _flatten(item)]
    return [] if value is None else [str(value)]


def _overlap(wanted: set[str], annotation: Annotation) -> int:
    """重叠度 = 当前问题与这条沉淀共享的 token 数(0 表示不相关)。"""
    return len(wanted & _tokens(_searchable(annotation)))
