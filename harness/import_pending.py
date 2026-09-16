"""待确认包的读写与清理(datasets/.pending/<id>/)。

从 harness/import_confirm.py 迁出(那边到行数上限了)。分工:本模块管**文件与只读
查询** —— 读包、列待确认、取列取值、量覆盖缺口、放弃、超时清理;
import_confirm.py 管两道闸门与落位。两者都是纯逻辑、零 Streamlit。

包的结构(由 harness/importer.py 写入):

    datasets/.pending/<id>/
    ├── dataset.yaml          # 清单(落位时原样搬走)
    ├── semantic.draft.yaml   # 自动推断的草稿地图(向导编辑的输入)
    ├── stats.json            # 列统计:向导回显候选列与推断判据
    └── data/fact.parquet
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

from harness.datasets import (
    DEFAULT_DATA_DIRNAME,
    DEFAULT_DATASETS_DIRNAME,
    MANIFEST_NAME,
    SEMANTIC_NAME,
    DatasetInfo,
    discover_datasets,
)
from harness.import_metric import quote_literal
from harness.importer import (
    DRAFT_NAME,
    STATS_NAME,
    UNCONFIRMED_FIELD,
    ConfirmError,
    pending_dir,
)

__all__ = [
    "DEFAULT_MAX_AGE_HOURS", "PendingPackage", "cleanup_stale", "count_uncovered",
    "date_range", "discard", "list_level_values", "list_pending", "list_unconfirmed",
    "load_pending", "load_placed", "read_manifest", "table_rows",
]

# 「按维度取值拆分」一次最多取多少个取值:取值可能很多(用户也能选非维度列),
# 不设上限会把界面灌爆。
VALUE_LIMIT = 50

# 待确认包的超时时间:中断不保留(没有「待确认列表」这种 UI),超时就当放弃
DEFAULT_MAX_AGE_HOURS = 24


@dataclass(frozen=True)
class PendingPackage:
    """一份待确认的导入包(datasets/.pending/<id>/)。"""

    id: str
    title: str
    root: Path
    data_dir: Path
    draft: dict      # 自动推断出的草稿语义层(向导编辑的输入)
    stats: dict      # 列统计:向导回显候选列与推断判据用


def load_pending(dataset_id: str, datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME
                 ) -> PendingPackage:
    """读取一份待确认包;不存在 / 已被清理 / 草稿损坏 -> ConfirmError。

    异常:
        ConfirmError: 待确认目录或草稿语义层缺失、草稿不是映射。
    """
    root = pending_dir(datasets_dir) / dataset_id
    draft_path = root / DRAFT_NAME
    if not draft_path.is_file():
        raise ConfirmError(f"待确认数据包不存在或已被清理:{root}")
    draft = _load_yaml(draft_path)
    if not isinstance(draft, Mapping):
        raise ConfirmError(f"草稿语义层不是映射:{draft_path}")
    manifest = _load_yaml(root / MANIFEST_NAME)
    return PendingPackage(
        id=dataset_id,
        title=str(manifest.get("title") or dataset_id),
        root=root,
        data_dir=root / DEFAULT_DATA_DIRNAME,
        draft=dict(draft),
        stats=_load_json(root / STATS_NAME),
    )


def load_placed(dataset_id: str, datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME
                ) -> PendingPackage:
    """把**已落位但未确认**的数据集读成向导输入(它的 semantic.yaml 就是草稿)。

    补确认之所以可以这么做:它管的仍然是「地图的诞生」—— 门槛是清单里的 unconfirmed
    标记(这份地图从未经过人工确认),与「重新导入」的差别只是不用再传一次文件。

    与 load_pending 的关键差别:这里**不搬动任何文件**,所以用户在向导里放弃不会有
    数据损失(不像 pending 包,晾久了会被超时清理掉)。

    异常:
        ConfirmError: 数据集不存在、没有语义层、或**已经确认过**了。
    """
    root = Path(datasets_dir) / dataset_id
    manifest = _load_yaml(root / MANIFEST_NAME)
    if manifest.get(UNCONFIRMED_FIELD) is not True:
        raise ConfirmError(f"数据集 {dataset_id!r} 没有「未确认」标记,前端不提供编辑入口")
    draft = _load_yaml(root / SEMANTIC_NAME)
    if not isinstance(draft, Mapping):
        raise ConfirmError(f"语义层读不动:{root / SEMANTIC_NAME}")
    return PendingPackage(id=dataset_id, title=str(manifest.get("title") or dataset_id),
                          root=root, data_dir=root / DEFAULT_DATA_DIRNAME,
                          draft=dict(draft), stats={})


def read_manifest(datasets_dir: str | Path, dataset_id: str) -> dict:
    """读清单原文(补确认时要保住 title 等既有字段,只摘掉未确认标记)。"""
    return _load_yaml(Path(datasets_dir) / dataset_id / MANIFEST_NAME)


def list_pending(datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME) -> list[str]:
    """待确认包的 id(按 id 排序);暂存区不存在返回空列表。"""
    base = pending_dir(datasets_dir)
    if not base.is_dir():
        return []
    return sorted(child.name for child in base.iterdir()
                  if (child / DRAFT_NAME).is_file())


def list_unconfirmed(datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME) -> list[DatasetInfo]:
    """已落位、但从未经过人工确认的数据集(跳过确认的那批)。

    供「以后可以回来补确认」的入口用:它们能被分析,但地图没过人工闸门。
    """
    return [info for info in discover_datasets(datasets_dir) if info.unconfirmed]


def list_level_values(data_dir: str | Path, table: str, column: str,
                      limit: int = VALUE_LIMIT) -> list[str]:
    """取某列的实际取值(去重、排序、**带 LIMIT**),供「按维度取值拆分」勾选。

    LIMIT 是硬要求:维度候选虽然是低基数列,但用户也能指定别的列,没有上限就会
    把整个界面灌满。列不存在 / 表读不动 -> 返回空列表(向导据此不给勾选,而不是崩掉)。
    """
    path = Path(data_dir) / f"{table}.parquet"
    if not path.is_file() or not column:
        return []
    quoted = _ident(column)
    rows = _query(
        f'SELECT DISTINCT "{quoted}" AS v FROM read_parquet(?) '
        f'WHERE "{quoted}" IS NOT NULL ORDER BY v LIMIT ?',
        [path.as_posix(), int(limit)])
    return [str(row[0]) for row in rows]


def count_uncovered(data_dir: str | Path, table: str, column: str,
                    values: Iterable[str]) -> int | None:
    """勾了这些取值之后,还有多少行**落不进任何一个子项**,问不出来时返回 None。

    为什么要问:子项的口径是 `col = 'v'`,而 SQL 里 NULL 不等于任何值 —— 没被勾选的
    取值和 NULL 行既不属于父指标也不属于任何子项,Σ子项 ≠ 父指标。这是口径事实,
    界面必须把它摆给用户看,不能替他决定(补一个「其它」子项是业务决策)。

    返回 None 表示「问不出来」(列不存在 / 表读不动 / 没勾取值),与「缺口为 0」区分开。
    """
    picked = [str(value) for value in values if str(value) != ""]
    path = Path(data_dir) / f"{table}.parquet"
    if not path.is_file() or not column or not picked:
        return None
    quoted = _ident(column)
    listed = ", ".join(quote_literal(value) for value in picked)
    rows = _query(
        f'SELECT count(*) FROM read_parquet(?) '
        f'WHERE "{quoted}" IS NULL OR "{quoted}" NOT IN ({listed})',
        [path.as_posix()])
    return int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else None


def date_range(data_dir: str | Path, fact_table: str,
               date_field: str) -> tuple[str, str] | None:
    """数据的时间范围(冒烟查询要一个真实窗口);表不存在或没有可用日期值返回 None。"""
    path = Path(data_dir) / f"{fact_table}.parquet"
    if not path.is_file() or not fact_table or not date_field:
        return None
    rows = _query(f'SELECT min("{_ident(date_field)}"), max("{_ident(date_field)}") '
                  "FROM read_parquet(?)", [path.as_posix()])
    if not rows or rows[0][0] is None or rows[0][1] is None:
        return None
    return str(rows[0][0]), str(rows[0][1])


def table_rows(data_dir: str | Path, table: str) -> int | None:
    """表的行数(向导第 ① 步回显「这张表有多少行」);读不到返回 None。"""
    path = Path(data_dir) / f"{table}.parquet"
    if not path.is_file() or not table:
        return None
    rows = _query("SELECT count(*) FROM read_parquet(?)", [path.as_posix()])
    return int(rows[0][0]) if rows and rows[0] else None


def discard(dataset_id: str, datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME) -> None:
    """放弃本次导入:直接删掉待确认包(向导上的「放弃」按钮),不落位。"""
    shutil.rmtree(pending_dir(datasets_dir) / dataset_id, ignore_errors=True)


def cleanup_stale(datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME,
                  max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
                  keep: Iterable[str] = ()) -> list[str]:
    """删掉超过 max_age_hours 还没落位的待确认包,返回被删的 id。

    中断不保留(已选定的取舍:不做「待确认列表」这种 UI)。判据是暂存目录自身的
    mtime —— 一次导入只写一次,它等价于「这份包是什么时候传上来的」。

    keep:本次会话正在编辑的包 id,永不删 —— 用户可能把向导晾了半天(超过 max_age),
    按 mtime 删掉他正在改的那份,是这套策略里唯一真正会伤到人的情形。
    """
    cutoff = time.time() - max_age_hours * 3600
    protected = {str(item) for item in keep}
    removed = []
    for name in list_pending(datasets_dir):
        if name in protected:
            continue
        root = pending_dir(datasets_dir) / name
        try:
            if root.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue                       # 正在被别人删 / 权限不足:跳过,不报错
        shutil.rmtree(root, ignore_errors=True)
        removed.append(name)
    return removed


# ---------------------------------------------------------------------------
# 内部工具:只读查询与文件读取
# ---------------------------------------------------------------------------
def _query(sql: str, params: list) -> list[tuple]:
    """跑一条只读查询取回全部行;DuckDB 层面的错误一律当成「取不到」返回空列表。"""
    con = duckdb.connect()
    try:
        return con.execute(sql, params).fetchall()
    except duckdb.Error:
        return []
    finally:
        con.close()


def _ident(name: str) -> str:
    """标识符进双引号:内部的引号加倍(列名带引号时不这么写会拼出非法 SQL)。"""
    return str(name).replace('"', '""')


def _load_yaml(path: Path) -> dict:
    """读 YAML;读不动 / 语法错 / 顶层不是映射一律当空字典(缺字段由调用方兜底)。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _load_json(path: Path) -> dict:
    """读 JSON 统计文件;缺失 / 损坏当空字典(向导只是少显示判据,不该整页崩)。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}