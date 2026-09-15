"""语义收集 A 档 CLI(§6.1):数据目录 -> 语义层草稿 semantic.draft.yaml。

用法:
    python scripts/profile_data.py --data-dir <目录> --out <semantic.draft.yaml>
    python scripts/profile_data.py --dataset ecommerce-demo     # 默认输出到数据集包内

流程:用 DuckDB DESCRIBE + 聚合现算每表每列的统计(类型 / 基数 / 空值率 /
唯一性 / 数值列的时间单调性)→ attribution.profile.suggest_semantic_draft
→ 写 YAML。草稿带「TODO: 人工确认」标记,不是最终语义层——**人工确认前
不要直接当 semantic.yaml 用**(它不认识业务口径、日历、caveats)。

数值列的「时间单调性」探测(半可加候选信号,§6.1):若表里存在日期列,按日期
升序把该数值列聚合后检查「晚日期的值 ≥ 早日期」是否普遍成立——余额/累计型
列会命中,建议 time_aggregation: last。没有日期列的表跳过该探测。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import duckdb
import yaml

# 直接 `python scripts/profile_data.py` 运行时,项目根不在搜索路径上,先补上
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 数值类型集合从草稿模块复用:被探测单调性的列 = 会被当成指标候选的列,两边必须同源
from attribution.profile import _NUMERIC_TYPES  # noqa: E402
from attribution.profile import suggest_semantic_draft  # noqa: E402
from harness.datasets import DatasetError  # noqa: E402
from scripts.eval_common import dataset_package, fail  # noqa: E402

__all__ = ["gather_stats", "main"]

_TODO = "TODO: 人工确认"
_DRAFT_NAME = "semantic.draft.yaml"
_PARQUET_GLOB = "*.parquet"
_MONOTONIC_RATIO = 0.9                    # 相邻日对子里「不回落」的比例下限(草稿级经验值)
_DATE_TYPE_PREFIXES = ("DATE", "TIMESTAMP")
_DATE_TOKENS = frozenset({"DATE", "DAY", "TIME", "TS", "DT", "MONTH", "YEAR"})
_HINT = f"{_TODO}: 本文件由 scripts/profile_data.py 依据表结构统计自动推断"


def gather_stats(data_dir: str) -> dict[str, dict[str, dict]]:
    """DuckDB 现算每表每列的统计(形状见 attribution.profile.suggest_semantic_draft)。

    只读:不开写、不缓存。统计缺失的列(如空表)给保守缺省,不抛错。
    """
    directory = Path(data_dir)
    if not directory.is_dir():
        raise ValueError(f"数据目录不存在:{directory}")
    files = {path.stem: path for path in sorted(directory.glob(_PARQUET_GLOB))}
    connection = duckdb.connect()          # 内存库:只读数据,不落盘、不缓存
    try:
        return {table: _table_stats(connection, str(path)) for table, path in files.items()}
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    """CLI:--data-dir 与 --dataset 二选一,--out 可选;退出码 0 正常 / 2 用法错误。"""
    parser = argparse.ArgumentParser(
        prog="profile_data.py", description="表结构统计 -> 语义层草稿 semantic.draft.yaml")
    parser.add_argument("--data-dir", metavar="目录", help="parquet 数据目录(与 --dataset 二选一)")
    parser.add_argument("--dataset", metavar="id|目录", help="数据集 id 或数据集包目录")
    parser.add_argument("--out", metavar="文件", help=f"输出路径(默认 {_DRAFT_NAME})")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:              # 用法错误:argparse 退出码 2;--help 的 0 原样透传
        return int(exc.code or 0)
    if bool(args.data_dir) == bool(args.dataset):
        return fail("--data-dir 与 --dataset 必须且只能给一个")
    try:
        data_dir, out, dataset_id = _resolve(args)
        stats = gather_stats(str(data_dir))
    except (DatasetError, ValueError, OSError) as err:
        return fail(str(err))
    if not any(stats.values()):
        return fail(f"{data_dir} 里没有可读的 parquet 表(表要能 DESCRIBE、要有列)")
    draft = suggest_semantic_draft(stats)
    draft["dataset"] = dataset_id
    try:
        out.write_text(_dump(draft), encoding="utf-8")
    except OSError as err:
        return fail(f"写 {out} 失败:{err}")
    _print_summary(draft, stats, out)
    return 0


# ---------------------------------------------------------------------------
# 以下为内部实现:参数解析与输出
# ---------------------------------------------------------------------------
def _resolve(args: argparse.Namespace) -> tuple[Path, Path, str]:
    """参数 -> (数据目录, 输出路径, 数据集名);--dataset 走数据集包(默认写回包内)。"""
    if args.data_dir:
        return Path(args.data_dir), Path(args.out or _DRAFT_NAME), ""
    info = dataset_package(args.dataset)
    return info.data_dir, Path(args.out or info.root / _DRAFT_NAME), info.id


def _dump(draft: dict) -> str:
    """草稿 -> YAML 文本:头部注释带草稿标记,正文 safe_dump(键序保持推断顺序)。"""
    header = "\n".join(f"# {line}" for line in (
        f"{_HINT},**不是可评估的语义层**。",
        f"{_TODO}: 业务口径 / 日历 / caveats / 分解声明都不在其中(机器推断不出来)。",
        f"{_TODO}: 推断判据与近似口径见文末 _todo 列表,逐条核对后再合入 semantic.yaml。"))
    return header + "\n" + yaml.safe_dump(draft, allow_unicode=True, sort_keys=False)


def _print_summary(draft: dict, stats: dict, out: Path) -> None:
    """人读概要:既看得到推断出了什么,也看得到它没推断出什么。"""
    columns = sum(len(table) for table in stats.values())
    semi = [name for name, metric in draft["metrics"].items()
            if metric["type"] == "semi_additive"]
    print(f"扫描 {len(stats)} 张表 / {columns} 列 -> {out}")
    print(f"  事实表候选 {draft['fact_table'] or '(没找到)'};"
          f"日期字段候选 {draft['date_field'] or '(没找到)'}")
    print(f"  指标候选 {len(draft['metrics'])} 个,其中半可加 {len(semi)} 个"
          f"{(': ' + ', '.join(semi)) if semi else ''}")
    print(f"  维度候选 {len(draft['dimensions'])} 个:{'、'.join(draft['dimensions']) or '无'}")
    print(f"  待人工确认 {len(draft['_todo'])} 条:草稿不可直接当 semantic.yaml 用")


# ---------------------------------------------------------------------------
# 以下为内部实现:统计探测(表 -> 列 -> 类型 / 基数 / 空值率 / 唯一性 / 单调性)
# ---------------------------------------------------------------------------
def _table_stats(connection: duckdb.DuckDBPyConnection, path: str) -> dict[str, dict]:
    """单表:DESCRIBE 取列 -> 一次聚合算基数 / 空值率 / 唯一性 -> 数值列探时间单调性。"""
    columns = _describe(connection, path)
    if not columns:
        return {}                          # 读不成 parquet:该表整体缺失,不抛错
    counts = _column_counts(connection, path, [name for name, _type in columns])
    date_column = _date_column(columns)    # 探测用的时间轴(没有 -> 全部跳过探测)
    stats: dict[str, dict] = {}
    for name, type_name in columns:
        numeric = _main_type(type_name) in _NUMERIC_TYPES
        stats[name] = {**_measures(counts.get(name)), "type": type_name,
                       "monotonic": (_is_monotonic(connection, path, date_column, name)
                                     if numeric else False)}
    return stats


def _describe(connection: duckdb.DuckDBPyConnection, path: str) -> list[tuple[str, str]]:
    """DESCRIBE -> [(列名, 类型)];坏文件 / 不可读给空列表(调用方按「该表缺失」处理)。"""
    try:
        rows = connection.execute(
            f"DESCRIBE SELECT * FROM read_parquet({_literal(path)})").fetchall()
    except duckdb.Error:
        return []
    return [(str(row[0]), str(row[1])) for row in rows]


def _column_counts(connection: duckdb.DuckDBPyConnection, path: str,
                   names: list[str]) -> dict[str, tuple[int, int, int]]:
    """每列 (去重值, 行数, 空值数);批量查询失败就逐列退让,坏列留空(保守缺省)。"""
    try:
        row = connection.execute(_batch_sql(path, names)).fetchone() or ()
    except duckdb.Error:
        return {name: counts for name in names if (counts := _one_column(connection, path, name))}
    return {name: (int(row[1 + 2 * i] or 0), int(row[0] or 0), int(row[2 + 2 * i] or 0))
            for i, name in enumerate(names) if 2 + 2 * i < len(row)}


def _batch_sql(path: str, names: list[str]) -> str:
    """一条聚合拿下所有列:行数 + 每列的去重值与空值数(列多时省一轮往返)。"""
    parts = ["count(*) AS n"]
    for index, name in enumerate(names):
        quoted = _quote(name)
        parts.append(f"count(DISTINCT {quoted}) AS d{index}")
        parts.append(f"count(*) FILTER (WHERE {quoted} IS NULL) AS z{index}")
    return f"SELECT {', '.join(parts)} FROM read_parquet({_literal(path)})"


def _one_column(connection: duckdb.DuckDBPyConnection, path: str,
                name: str) -> tuple[int, int, int] | None:
    """单列退让查询(DISTINCT 对嵌套类型会失败);失败给 None = 该列统计缺失。"""
    quoted = _quote(name)
    try:
        row = connection.execute(
            f"SELECT count(*), count(DISTINCT {quoted}), "
            f"count(*) FILTER (WHERE {quoted} IS NULL) "
            f"FROM read_parquet({_literal(path)})").fetchone()
    except duckdb.Error:
        return None
    return (int(row[1] or 0), int(row[0] or 0), int(row[2] or 0)) if row else None


def _measures(counts: tuple[int, int, int] | None) -> dict[str, Any]:
    """(去重值, 行数, 空值数)-> 基数 / 空值率 / 唯一性;没有计数(空表)给保守缺省。"""
    cardinality, total, nulls = counts or (0, 0, 0)
    if total <= 0:
        return {"cardinality": 0, "null_rate": 0.0, "uniqueness": 0.0}
    return {"cardinality": cardinality, "null_rate": round(nulls / total, 6),
            "uniqueness": cardinality / total}


def _is_monotonic(connection: duckdb.DuckDBPyConnection, path: str, date_column: str,
                  column: str) -> bool:
    """按日期升序聚合该列后,「晚日期的值 ≥ 早日期」在 ≥ 90% 的相邻日对子里成立。

    只是半可加候选信号:余额 / 累计型列命中,普通流量列通常回落(§6.1)。
    """
    if not date_column or date_column == column:
        return False                       # 没有时间轴,或列自己就是时间轴:不判定
    day, value = _quote(date_column), _quote(column)
    sql = (f"WITH daily AS (SELECT TRY_CAST({day} AS DATE) AS d, SUM({value}) AS v "
           f"FROM read_parquet({_literal(path)}) "
           f"WHERE {day} IS NOT NULL AND {value} IS NOT NULL GROUP BY 1), "
           f"pairs AS (SELECT v, LAG(v) OVER (ORDER BY d) AS p FROM daily WHERE d IS NOT NULL) "
           f"SELECT count(*) FILTER (WHERE p IS NOT NULL), "
           f"count(*) FILTER (WHERE p IS NOT NULL AND v >= p) FROM pairs")
    try:
        row = connection.execute(sql).fetchone() or ()
    except duckdb.Error:
        return False                       # 聚合不了(非数值 / 类型不支持):不判定
    pairs, holding = (int(row[0] or 0), int(row[1] or 0)) if len(row) == 2 else (0, 0)
    return pairs > 0 and holding / pairs >= _MONOTONIC_RATIO


def _date_column(columns: list[tuple[str, str]]) -> str:
    """挑一个日期列做单调性探测:优先 DATE / TIMESTAMP 类型,其次列名带日期词元。

    与草稿模块的日期字段候选同思路(那边还要在多个候选里定序),但探测只求有一个时间轴。
    """
    typed = [name for name, type_name in columns
             if _main_type(type_name).startswith(_DATE_TYPE_PREFIXES)]
    named = [name for name, _type in columns if _tokens(name) & _DATE_TOKENS]
    return typed[0] if typed else (named[0] if named else "")


def _main_type(type_name: str) -> str:
    """类型名 -> 大写主类型(DECIMAL(18,2) -> DECIMAL)。"""
    return str(type_name).upper().split("(")[0].strip()


def _tokens(name: str) -> set[str]:
    """列名 -> 大写词元集合(按下划线分词,整个名字也算一个词元)。"""
    upper = name.upper()
    return {upper, *upper.split("_")}


def _quote(identifier: str) -> str:
    """SQL 标识符加双引号(列名可能带特殊字符;双写内部引号转义)。"""
    return '"' + str(identifier).replace('"', '""') + '"'


def _literal(text: str) -> str:
    """SQL 字符串字面量加单引号(Windows 路径含反斜杠,只转义单引号即可)。"""
    return "'" + str(text).replace("'", "''") + "'"


if __name__ == "__main__":
    sys.exit(main())
