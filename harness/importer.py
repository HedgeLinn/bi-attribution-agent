"""通用数据导入:CSV / Excel -> 自动识别列名 -> 可分析数据集包。

流程(复用现有链路):
    CSV/Excel -> pandas 读入 -> 列名 str 化 -> 日期列转真实 DATE
    -> 写 data/fact.parquet -> gather_stats + suggest_semantic_draft 生成语义草稿
    -> 补「派生维度(derived)」补齐单表维度 -> 写 datasets/<id>/ 数据集包

单表 CSV 的关键:suggest_semantic_draft 的维度推断只处理「非事实表且有主键」的
维度表,单表场景 dimensions 必为空。引擎原生支持派生维度(层级字段直接住在事实表
上,不 join),故这里把低基数文本列提升为 derived 维度,让新数据集立刻可下钻。

边界:
    - 本模块是纯逻辑:**零 Streamlit**、不做 UI,调用方(app/importer_ui.py)负责交互
    - 对外只暴露一个公共入口 import_upload(name, data, ...) -> dict(成功/失败都进 dict)
    - 不修改 attribution/ 的任何文件:识别规则全部复用它现有的推论函数
"""

from __future__ import annotations

import io
import re
import time
from pathlib import Path

import pandas as pd
import yaml

from attribution.profile import (
    _ID_TOKEN,
    _LOW_CARD,
    _PK_AT_LEAST,
    _STRONG_DATE_TOKENS,
    _kind,
    _parts,
    suggest_semantic_draft,
)
from harness.datasets import DEFAULT_DATA_DIRNAME, DEFAULT_DATASETS_DIRNAME
from scripts.profile_data import gather_stats

__all__ = ["ImporterError", "import_upload", "pick_date_column", "read_upload"]

# 数据集包身份:事实表固定写 parquet stem(坑 4);版本号必须是可评估值,不能是 draft
_FACT_TABLE = "fact"
_PARQUET_NAME = "fact.parquet"
_DATASET_VERSION = "import"

# 行数上限:防止超大文件拖垮统计与引擎;超出的行丢弃(首部优先)
_MAX_ROWS = 200_000

# id 格式:upload_<文件名>_<时间戳>;同秒同名冲突时追加 _2 / _3 …
_ID_PREFIX = "upload"
_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"

# 字符串日期判定阈值:该列可成功解析的比例不低于它,才算「像日期列」
_DATE_PARSE_RATIO = 0.9


class ImporterError(Exception):
    """导入失败:文件内容不满足可分析要求(空文件 / 无日期列 / 无数值列 / 解析失败)。"""


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
    """上传文件 -> 数据集包,返回统一结果 dict(成功 / 失败都不抛异常)。

    成功: {"ok": True, "id", "title", "fact_table", "date_field",
            "metrics", "dimensions", "rows"}
    失败: {"ok": False, "error", "code"?}
    code == "no_date_column" 时附带 "columns"[所有列名],供 UI 让用户挑时间字段。

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
        package_root = _write_package(frame, stem, date_field, Path(datasets_dir))
        return {
            "ok": True,
            "id": package_root.name,
            "title": _title_of(stem),
            "fact_table": _FACT_TABLE,
            "date_field": date_field,
            "metrics": _read_names(package_root / "semantic.yaml", "metrics"),
            "dimensions": _read_names(package_root / "semantic.yaml", "dimensions"),
            "rows": int(len(frame)),
        }
    except ImporterError as err:
        return {"ok": False, "error": str(err)}
    except (ValueError, OSError) as err:
        return {"ok": False, "error": str(err)}


# ---------------------------------------------------------------------------
# 内部实现:文件校验 / 数据落盘 / 语义层生成 / 数据集包写入
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
    """数据落盘 + 语义层生成 + 写数据集包;返回包根目录(调用方拿它当 id)。"""
    dataset_id = _unique_dataset_id(stem, datasets_dir)
    package_root = datasets_dir / dataset_id
    data_dir = package_root / DEFAULT_DATA_DIRNAME
    _write_data(frame, data_dir, date_field)
    stats = gather_stats(str(data_dir))
    semantic = _build_semantic(stats, dataset_id, date_field)
    if not semantic["metrics"]:
        raise ImporterError("未识别出任何数值指标列:至少需要一个数值列作为指标")
    manifest = {"id": dataset_id, "title": _title_of(stem)}
    package_root.mkdir(parents=True, exist_ok=True)
    (package_root / "semantic.yaml").write_text(
        yaml.safe_dump(semantic, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (package_root / "dataset.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return package_root


def _write_data(frame: pd.DataFrame, data_dir: Path, date_field: str) -> None:
    """日期列转换后写 parquet(ID 列名要能进 DuckDB,字符串日期要变成真 DATE)。"""
    work = frame.reset_index(drop=True)
    if date_field in work.columns:
        work[date_field] = pd.to_datetime(work[date_field], errors="coerce").dt.date
    data_dir.mkdir(parents=True, exist_ok=True)
    work.to_parquet(data_dir / _PARQUET_NAME, index=False)


def _build_semantic(stats: dict, dataset_id: str, date_field: str) -> dict:
    """草稿 -> 可评估语义层:改身份三处 + 删 _todo + 补派生维度(单表场景的关键)。"""
    draft = suggest_semantic_draft(stats)
    draft["dataset"] = dataset_id
    draft["dataset_version"] = _DATASET_VERSION    # 坑 1:不能空、不能 draft
    draft["fact_table"] = _FACT_TABLE              # 坑 4:必须等于 parquet stem
    draft["date_field"] = date_field               # 用最终确定的时间字段(含 override)
    _add_derived_dimensions(draft, stats, date_field)
    _unique_metric_names(draft, stats)             # 坑 5:指标 key 必须与列名错开(见函数 docstring)
    draft.pop("_todo", None)                       # 不留草稿标记:这是正式地图
    return draft


def _unique_metric_names(draft: dict, stats: dict) -> None:
    """指标 key 不得与事实表列名重合:草稿指标 key == 数值列名,depends_on 又指向同一列,
    引擎 load 时的 find_dependency_cycle 会把这当成「指标依赖自身」抛成环(演示数据集的
    指标 key 与列名刻意错开所以从不触发;这批自动地图必须自己归一)。撞名的 key 追加
    _sum 后缀——label / expression / depends_on 不动,对外展示仍是原列名。
    """
    fact_cols = set((stats.get(draft["fact_table"]) or {}).keys())
    metrics = draft["metrics"]
    for name in list(metrics):
        if name not in fact_cols:
            continue
        new = f"{name}_sum"
        while new in metrics:
            new = f"{new}_sum"
        metrics[new] = metrics.pop(name)


def _add_derived_dimensions(draft: dict, stats: dict, date_field: str) -> None:
    """低基数文本列 -> derived 维度(层级字段就在事实表上,引擎不 join 也能下钻)。

    过滤口径与 profile 的层级推断一致:只收基数 2~50 的文本列,剔除时间轴、代理键
    (唯一列 / ID 词元)与「像日期」的列。
    """
    fact_cols = stats.get(draft["fact_table"], {})
    dimensions = draft.setdefault("dimensions", {})
    for column, stat in fact_cols.items():
        if column == date_field:
            continue
        if _kind(stat) != "OTHER":
            continue
        if not (2 <= stat["cardinality"] <= _LOW_CARD):
            continue
        if stat["uniqueness"] >= _PK_AT_LEAST:
            continue
        if _ID_TOKEN in _parts(column):
            continue
        if _parts(column) & _STRONG_DATE_TOKENS:
            continue
        dimensions[column] = {"label": column, "type": "derived",
                              "hierarchy": [column]}


def _unique_dataset_id(stem: str, datasets_dir: Path) -> str:
    """生成不冲突的数据集 id:upload_<sanitized>_<时间戳>;已存在追加 _2 / _3 …"""
    base = f"{_ID_PREFIX}_{_sanitize_id(stem)}_{time.strftime(_TIMESTAMP_FORMAT)}"
    candidate, counter = base, 2
    while (datasets_dir / candidate).exists():
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
