"""通用数据导入:CSV / Excel -> 自动识别列名 -> **待确认**的数据集包。

流程(复用现有链路):
    CSV/Excel -> pandas 读入 -> 列名 str 化 -> 日期列转真实 DATE
    -> 写 data/fact.parquet -> gather_stats 出列统计
    -> build_semantic 出草稿语义层 -> 写 datasets/.pending/<id>/ 待确认包

**为什么落在 .pending/ 而不是 datasets/<id>/**:自动推断出的地图合法但可以错 ——
事实表判据是「行数最多」、指标只覆盖「数值列 -> SUM」、半可加只看单调性阈值 0.90。
实测:月度锯齿数据(月内累计、跨月归零)被判成 semi_additive + last,整窗值 128
vs 逐日真值 9,618,而生成的 YAML 装载校验一声不吭。所以导入只产出**待确认**包,
由 harness/import_confirm.py 在人工确认(或显式跳过)之后才落位成正式数据集。

单表 CSV 的关键:suggest_semantic_draft 的维度推断只处理「非事实表且有主键」的
维度表,单表场景 dimensions 必为空。引擎原生支持派生维度(层级字段直接住在事实表
上,不 join),故草稿把低基数文本列提升为 derived 维度,让新数据集立刻可下钻。

边界:
    - 本模块是纯逻辑:**零 Streamlit**、不做 UI,调用方(app/importer_ui.py)负责交互
    - 对外只暴露一个公共入口 import_upload(name, data, ...) -> dict(成功/失败都进 dict)
    - 不修改 attribution/ 的任何文件:识别规则全部复用它现有的推论函数
    - 不写 datasets/<id>/:落位是 import_confirm 的职责(全项目唯一能写它的地方)
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import time
from pathlib import Path

import pandas as pd
import yaml

from harness.datasets import DEFAULT_DATA_DIRNAME, DEFAULT_DATASETS_DIRNAME
from harness.import_draft import (
    DATASET_VERSION as _DATASET_VERSION,   # noqa: F401  测试与调用方从本模块取这些常量
    FACT_TABLE as _FACT_TABLE,             # noqa: F401
    PARQUET_NAME as _PARQUET_NAME,         # noqa: F401
    build_semantic,
)
from scripts.profile_data import gather_stats

__all__ = [
    "ConfirmError", "DRAFT_NAME", "ImporterError", "PENDING_DIRNAME", "STATS_NAME",
    "dump_yaml", "import_upload", "pending_dir", "pick_date_column", "read_upload",
]

# 行数上限:防止超大文件拖垮统计与引擎;超出的行丢弃(首部优先)
_MAX_ROWS = 200_000

# 未确认包的暂存区(datasets/.pending/)。目录名以点开头、且清单不在它自己那一层,
# 所以它整块逃过 harness.datasets 的发现(.pending/ 下没有 dataset.yaml)。
PENDING_DIRNAME = ".pending"

# 草稿语义层的文件名:刻意**不叫** semantic.yaml —— 它必须逃过 tests/conftest.py 的
# 语义层扫描(datasets/*/semantic.yaml 是非递归 glob,.pending/<id>/semantic.draft.yaml
# 匹配不到),否则未确认的草稿会污染防回退词汇表。目录隔离已是第一道保险,这是第二道。
DRAFT_NAME = "semantic.draft.yaml"

# 列统计:向导要拿它回显候选列与「行数最多 / 单调」这类推断判据
STATS_NAME = "stats.json"

# 清单里的未确认标记。缺省即「已确认」——4 套手写数据集没有这个字段,不该被判成未确认
# (harness/datasets._build_info 只读已知字段,未知字段静默忽略,所以加字段向后兼容)。
UNCONFIRMED_FIELD = "unconfirmed"

# id 格式:upload_<文件名>_<时间戳>;同秒同名冲突时追加 _2 / _3 …
_ID_PREFIX = "upload"
_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"

# 字符串日期判定阈值:该列可成功解析的比例不低于它,才算「像日期列」
_DATE_PARSE_RATIO = 0.9


class ImporterError(Exception):
    """导入失败:文件内容不满足可分析要求(空文件 / 无日期列 / 无数值列 / 解析失败)。"""


class ConfirmError(Exception):
    """确认流程的输入不合法:答案自相矛盾、表达式拆不出来、或落位闸门未通过。

    定义在本模块(而不是 import_answers / import_confirm)是因为确认族里**有两个
    模块同时需要它**,而它们互相之间还有依赖 —— 放最低层不会成环。落位是「全有或
    全无」:抛这个异常时磁盘上不会有半份数据集包。
    """


def read_upload(name: str, data: bytes) -> pd.DataFrame:
    """按扩展名分派 pandas 读入;列名一律 str 化(整数列名 / Unnamed: 0 会打崩推断)。

    坏文件 / 不支持的类型统一转 ImporterError,不把解析异常吐给上层。
    """
    lowered = (name or "").lower()
    try:
        if lowered.endswith(".csv"):
            frame = pd.read_csv(io.BytesIO(data))
        elif lowered.endswith((".xlsx", ".xls")):
            frame = pd.read_excel(io.BytesIO(data))
        else:
            raise ImporterError(
                f"不支持的文件类型:{name or '(未命名)'}(仅支持 csv / xlsx / xls)")
    except ImporterError:
        raise
    except Exception as err:  # noqa: BLE001  解析异常全部归一,由调用方展示
        raise ImporterError(f"文件解析失败:{err}") from err
    frame.columns = [str(column) for column in frame.columns]
    return frame


def pick_date_column(df: pd.DataFrame) -> str:
    """自动识别时间列:已是 datetime 类型的列直接命中;字符串列试解析,命中率高才收。

    数值列一律跳过(「年份」这类分量不是时间轴,与 profile 的日期推断同一口径)。
    """
    for column in df.columns:
        series = df[column]
        if pd.api.types.is_datetime64_any_dtype(series):
            return column
        # pandas 3.x 的字符串列 dtype 是 str(不是 object):用「排除结构化类型」兜住
        if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
            continue
        try:
            parsed = pd.to_datetime(series, errors="coerce")
        except (TypeError, ValueError):
            continue
        total = int(len(series))
        if total > 0 and int(parsed.notna().sum()) / total >= _DATE_PARSE_RATIO:
            return column
    return ""


def import_upload(name: str, data: bytes,
                  datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME,
                  date_field_override: str | None = None) -> dict:
    """上传文件 -> **待确认**数据集包,返回统一结果 dict(成功 / 失败都不抛异常)。

    成功: {"ok": True, "id", "title", "fact_table", "date_field",
            "metrics", "dimensions", "rows"}
    失败: {"ok": False, "error", "code"?}
    code == "no_date_column" 时附带 "columns"[所有列名],供 UI 让用户挑时间字段。

    成功只代表包写进了 datasets/.pending/<id>/,**还不是**一个可分析数据集 ——
    落位要等 harness.import_confirm.commit(人工确认)或 skip(显式跳过)。
    返回键与「直接落位」时代保持一致,调用方(UI / 测试)不必跟着改。

    date_field_override:无日期列时用户在界面指定的一列(字符串日期自动转 DATE)。
    """
    try:
        stem = _stem_of(name)
        frame = read_upload(name, data)
        _reject_empty(frame)
        frame = _limited(frame)
        date_field = date_field_override or pick_date_column(frame)
        if not date_field:
            return {"ok": False, "code": "no_date_column",
                    "columns": list(frame.columns)}
        if date_field_override and date_field_override not in frame.columns:
            raise ImporterError(f"指定的时间字段 {date_field_override!r} 不在数据列中")
        pending_root = _write_package(frame, stem, date_field, Path(datasets_dir))
        return {
            "ok": True,
            "id": pending_root.name,
            "title": _title_of(stem),
            "fact_table": _FACT_TABLE,
            "date_field": date_field,
            "metrics": _read_names(pending_root / DRAFT_NAME, "metrics"),
            "dimensions": _read_names(pending_root / DRAFT_NAME, "dimensions"),
            "rows": int(len(frame)),
        }
    except ImporterError as err:
        return {"ok": False, "error": str(err)}
    except (ValueError, OSError, yaml.YAMLError) as err:
        return {"ok": False, "error": str(err)}


def pending_dir(datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME) -> Path:
    """未确认包的暂存区根目录(datasets/.pending/);只给路径,不创建它。"""
    return Path(datasets_dir) / PENDING_DIRNAME


def dump_yaml(path: Path, payload) -> None:
    """原子写 YAML:allow_unicode 保住中文,键序即文件行序(sort_keys=False)。

    先写 .tmp 再 os.replace(同盘符下是原子操作):中途崩了要么是旧内容、要么是
    新内容,不会留下半份地图被引擎当合法地图装载。
    """
    text = yaml.safe_dump(dict(payload), allow_unicode=True, sort_keys=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 内部实现:文件校验 / 数据落盘 / 待确认包写入
# ---------------------------------------------------------------------------
def _stem_of(name: str) -> str:
    """取文件名主干的空白清理版(无扩展名);空名给空串。"""
    return (name or "").split("/")[-1].rsplit(".", 1)[0].strip() if name else ""


def _title_of(stem: str) -> str:
    """数据集标题:文件名主干;空名给占位。"""
    return stem or "导入数据集"


def _reject_empty(frame: pd.DataFrame) -> None:
    """空文件 / 没有任何列:直接判失败(不生成数据集包)。"""
    if frame is None or frame.empty or not len(frame.columns):
        raise ImporterError("文件为空或没有可识别的数据")


def _limited(frame: pd.DataFrame) -> pd.DataFrame:
    """超限截断:只保留前 _MAX_ROWS 行,防止统计与引擎被超大文件拖垮。"""
    return frame.head(_MAX_ROWS) if len(frame) > _MAX_ROWS else frame


def _write_package(frame: pd.DataFrame, stem: str, date_field: str,
                   datasets_dir: Path) -> Path:
    """数据落盘 + 草稿生成 + 写待确认包;返回包根目录(调用方拿它当 id)。

    **写的是 .pending/<id>/ 而不是 <id>/**:这份地图还没过人工闸门。清单也一起写进去
    (落位时原样搬走),但它在 .pending/ 里面,发现不了,不会被选中。

    失败即清场:统计或草稿生成失败(如一个数值列都没有)时把整个待确认目录删掉 ——
    只在 .pending 里留一个半截目录,下次清理之前都会混在待确认列表里。
    """
    dataset_id = _unique_dataset_id(stem, datasets_dir)
    pending_root = pending_dir(datasets_dir) / dataset_id
    try:
        data_dir = pending_root / DEFAULT_DATA_DIRNAME
        _write_data(frame, data_dir, date_field)
        stats = gather_stats(str(data_dir))
        semantic = build_semantic(stats, dataset_id, date_field)
        if not semantic["metrics"]:
            raise ImporterError("未识别出任何数值指标列:至少需要一个数值列作为指标")
        pending_root.mkdir(parents=True, exist_ok=True)
        dump_yaml(pending_root / DRAFT_NAME, semantic)
        dump_yaml(pending_root / "dataset.yaml",
                  {"id": dataset_id, "title": _title_of(stem)})
        (pending_root / STATS_NAME).write_text(
            json.dumps(stats, ensure_ascii=False), encoding="utf-8")
    except (ImporterError, OSError, ValueError):
        shutil.rmtree(pending_root, ignore_errors=True)
        raise
    return pending_root


def _write_data(frame: pd.DataFrame, data_dir: Path, date_field: str) -> None:
    """日期列转换后写 parquet(ID 列名要能进 DuckDB,字符串日期要变成真 DATE)。"""
    work = frame.reset_index(drop=True)
    if date_field in work.columns:
        work[date_field] = pd.to_datetime(work[date_field], errors="coerce").dt.date
    data_dir.mkdir(parents=True, exist_ok=True)
    work.to_parquet(data_dir / _PARQUET_NAME, index=False)




def _unique_dataset_id(stem: str, datasets_dir: Path) -> str:
    """生成不冲突的数据集 id:upload_<sanitized>_<时间戳>;已存在追加 _2 / _3 …

    暂存区与发布区**都要查**:同一秒上传第二份同名文件时,只看其中一边就会让后一份
    盖掉前一份(暂存区里那个还没落位,发布区里那个已经落位了)。
    """
    base = f"{_ID_PREFIX}_{_sanitize_id(stem)}_{time.strftime(_TIMESTAMP_FORMAT)}"
    candidate, counter = base, 2
    while ((datasets_dir / candidate).exists()
           or (pending_dir(datasets_dir) / candidate).exists()):
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def _sanitize_id(stem: str) -> str:
    """id 只留字母数字下划线连字符;中文(可能出现在文件名里)等一律转下划线。"""
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", stem or "").strip("_")
    return text or "import"


def _read_names(semantic_path: Path, section: str) -> list[str]:
    """读回语义层某段(metrics / dimensions)的键名,供成功结果回显。"""
    raw = yaml.safe_load(semantic_path.read_text(encoding="utf-8"))
    names = raw.get(section) or {}
    return sorted(str(name) for name in names)