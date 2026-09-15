"""语义收集 A 档(§6.1):从表结构统计推断语义层草稿。

纯函数:入参是「表 -> 列 -> 统计」的字典(由 scripts/profile_data.py 用 DuckDB
DESCRIBE + 聚合现算),出参是 semantic.draft.yaml 的 dict 形状——**本模块不碰
DuckDB、不做 IO**。草稿里所有推断都带 `# TODO: 人工确认` 形态的标记(草稿是
给人工写语义层时的起点,不是自动生成的最终地图)。

核心产物:
    - 指标草稿:数值列 -> SUM 候选(depends_on 含该列)
    - 半可加候选:单调累计/余额型数值列 -> 建议 type: semi_additive + time_aggregation: last
      (这条直接防 §1.3 的静默错误——§6.1 点名它是本工具存在的首要理由)
    - 维度草稿:低基数文本/枚举列 -> 候选层级;唯一性 = 1.0 的列 -> 候选主键;
      包含率高的列 -> 候选外键
    - 日期列:识别日期类型与粒度

受 test_engine_has_no_hardcoded_vocabulary 的 AST 扫描约束:可执行代码里
不得出现数据集词汇,docstring 与注释里的举例除外。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["suggest_semantic_draft"]

# 草稿标记:机器推断一律带它,人工确认后删掉
_TODO = "TODO: 人工确认"

# 推断阈值:草稿级经验值,人工确认时可自由改
_SCHEMA_VERSION = "2.0"      # 与 datasets/*/semantic.yaml 的 schema_version 对齐
_DRAFT_VERSION = "draft"     # 草稿身份:不是可评估的地图版本(§3.6①)
_LOW_CARD = 50               # 候选层级成员的基数上限(超过它就不是层级而是成员标识)
_MIN_LEVEL_CARD = 2          # 单一取值的列没有区分度:不做层级、不算外键
_PK_AT_LEAST = 1.0 - 1e-9    # 唯一性达标即候选主键(它由「去重值 / 行数」除出来)
_MIN_CARD = 1                # 空列不参与推断(宁缺毋滥)

# 类型名与列名词元一律按**大写**比较:DESCRIBE 返回的就是大写类型名;更要紧的是,大写常量
# 天然不与语义层里的(小写)列名词汇相撞——防回退测试会扫描本模块的标识符与字符串字面量
# (§3.6⑤),所以可执行代码里不出现任何具体数据集的列名。
_NUMERIC_TYPES = frozenset({
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT",
    "UINTEGER", "UBIGINT", "FLOAT", "REAL", "DOUBLE", "DECIMAL", "NUMERIC",
})
_DATE_TYPE_PREFIXES = ("DATE", "TIMESTAMP")
_STRONG_DATE_TOKENS = frozenset({"DATE", "DAY"})     # 几乎一定是时间轴
_WEAK_DATE_TOKENS = frozenset({"TIME", "TS", "DT", "MONTH", "YEAR"})
_ID_TOKEN, _NAME_TOKEN = "ID", "NAME"
_TABLE_PREFIXES = ("DIM", "DIMENSION")               # 维度表的常见命名前缀
_DATE_BY_NAME_REASON = "列名含日期词元,但物理类型是字符串(能否解析为日期需现场核对)"


def suggest_semantic_draft(table_stats: dict[str, dict[str, dict[str, Any]]]
                           ) -> dict[str, Any]:
    """表统计 -> 语义层草稿 dict(可直接 yaml.safe_dump 成 semantic.draft.yaml)。

    入参形状:
        {表名: {列名: {
            "type": str,          # DuckDB 类型名(INTEGER / VARCHAR / DATE ...)
            "cardinality": int,   # 去重值个数
            "null_rate": float,   # 空值率 0~1
            "uniqueness": float,  # 去重值 / 行数(候选主键 = 1.0)
            "monotonic": bool,    # 数值列是否随时间单调不减(半可加候选信号)
        }}}
    出参形状(语义层 schema v2 的子集,全部推断处带 _TODO 注释块):
        {schema_version, dataset, dataset_version: "draft",
         fact_table(候选), date_field(候选), metrics, dimensions, _todo: [...]}
    任何统计缺失 / 形状非法都不抛错——草稿宁缺毋滥,把疑点写进 _todo。
    """
    tables = _sanitize(table_stats)
    fact, rows = _fact_table(tables)
    date_info = _pick_date_field(tables.get(fact, {}))
    keys = {table: _pick_key(columns) for table, columns in tables.items()}
    fks = _find_fks(tables, fact, keys)
    metrics = _build_metrics(tables, fact, date_info[0], keys)
    return {"schema_version": _SCHEMA_VERSION, "dataset": "",   # 数据集名由调用方(CLI)填
            "dataset_version": _DRAFT_VERSION, "fact_table": fact, "date_field": date_info[0],
            "metrics": metrics, "dimensions": _build_dimensions(tables, fact, keys, fks),
            "_todo": _build_notes(rows, fact, date_info, keys, fks, metrics)}


# --- 入参清洗与统计条目上的小工具:过了 _sanitize 这道门,内部只见到干净的数字 ---
def _sanitize(table_stats: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """入参 -> 「表 -> 列 -> 五个统计量」;形状非法的条目直接丢掉,缺统计给保守缺省。"""
    if not isinstance(table_stats, Mapping):
        return {}
    return {
        table: {column: {"type": str(stat.get("type") or ""),
                         "cardinality": int(_as_number(stat.get("cardinality"))),
                         "null_rate": _as_number(stat.get("null_rate")),
                         "uniqueness": _as_number(stat.get("uniqueness")),
                         "monotonic": stat.get("monotonic") is True}
                for column, stat in columns.items()
                if isinstance(column, str) and isinstance(stat, Mapping)}
        for table, columns in table_stats.items()
        if isinstance(table, str) and isinstance(columns, Mapping)
    }


def _as_number(value: Any) -> float:
    """宽松取数(数字字符串也认);取不到给 0.0。bool 不当数字:免得 True 变成 1.0。"""
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _kind(stat: Mapping[str, Any]) -> str:
    """统计条目 -> 类型大类 DATE / NUMBER / OTHER(DECIMAL(18,2) 归到 DECIMAL)。"""
    name = str(stat.get("type") or "").upper().split("(")[0].strip()
    if name in _NUMERIC_TYPES:
        return "NUMBER"
    return "DATE" if name.startswith(_DATE_TYPE_PREFIXES) else "OTHER"


def _parts(column: str) -> set[str]:
    """列名 -> 大写词元集合(按下划线分词,整个名字也算一个词元;不用子串匹配)。"""
    upper = column.upper()
    return {upper, *upper.split("_")}


# --- 表级推断:行数 / 事实表候选 / 日期字段候选 / 候选主键与外键 ---
def _fact_table(tables: Mapping[str, Mapping[str, Mapping[str, Any]]]
                ) -> tuple[str, dict[str, int]]:
    """候选事实表(行数最多的一张)+ 每表行数;行数由「去重值 / 唯一性」反推(统计里没有)。

    行数 ≥ 基数(唯一性 ≤ 1),下界由 max(cardinality, …) 兜住;并列按名字定序,同一份
    统计永远给同一份草稿。
    """
    rows = {table: max([0] + [max(s["cardinality"], int(round(s["cardinality"] / s["uniqueness"])))
                              for s in columns.values()
                              if s["cardinality"] > 0 and s["uniqueness"] > 0])
            for table, columns in tables.items()}
    candidates = [table for table, count in rows.items() if count > 0]
    fact = sorted(candidates, key=lambda table: (-rows[table], table))[0] if candidates else ""
    return fact, rows


def _pick_date_field(columns: Mapping[str, Mapping[str, Any]]) -> tuple[str, str]:
    """日期字段候选 + 判据;优先级「类型 > 强词元 > 弱词元」,同级取空值少、名字短的。

    判据要区分「类型就是时间」与「列名像、物理类型仍是字符串」——后者是这份草稿最容易
    被误用的一处:字符串日期看着能用,排序却不等于时间序,必须人工核对。
    """
    best: tuple[tuple[int, float, int], str, str] | None = None
    for column, stat in columns.items():
        # rank:3 = 类型就是日期 / 时间,2 = 列名带强日期词元,1 = 弱词元,0 = 不像
        if stat["cardinality"] < _MIN_CARD:
            continue
        if _kind(stat) == "NUMBER":
            continue                     # 数值列不是时间轴(「年 / 月」这类分量另算)
        rank = (3 if _kind(stat) == "DATE" else 2 if _parts(column) & _STRONG_DATE_TOKENS
                else 1 if _parts(column) & _WEAK_DATE_TOKENS else 0)
        if rank == 0:
            continue
        order = (rank, -stat["null_rate"], -len(column))
        if best is None or order > best[0]:
            best = (order, column, "类型本身是日期 / 时间类型" if rank == 3
                    else _DATE_BY_NAME_REASON)
    return (best[1], best[2]) if best else ("", "")


def _pick_key(columns: Mapping[str, Mapping[str, Any]]) -> str:
    """候选主键:唯一性 = 1.0 的列;优先带 ID 词元的代理键,其次基数最大,再按名字。"""
    found = [c for c, s in columns.items()
             if s["uniqueness"] >= _PK_AT_LEAST and s["cardinality"] >= _MIN_CARD]
    return sorted(found, key=lambda c: (
        _ID_TOKEN not in _parts(c), -columns[c]["cardinality"], c))[0] if found else ""


def _find_fks(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], fact: str,
              keys: Mapping[str, str]) -> list[tuple[str, str, str]]:
    """候选外键:(源表, 列名, 目标表),目标表以该列为候选主键。

    统计里**没有包含率**,用它仅剩的必要条件近似:同名、目标列唯一性 1.0、源列基数 ≤
    目标列基数(否则一定包含不下)。精确包含率要现场查数据。
    """
    found: list[tuple[str, str, str]] = []
    for target in sorted(keys):
        primary = keys[target]
        if not primary or target == fact:
            continue
        target_card = tables[target][primary]["cardinality"]
        for source, columns in sorted(tables.items()):
            stat = columns.get(primary)
            if source == target or stat is None:
                continue
            if stat["uniqueness"] >= _PK_AT_LEAST:
                continue                  # 自身唯一 -> 它自己就是键,不是指向别人的外键
            if _MIN_LEVEL_CARD <= stat["cardinality"] <= target_card:
                found.append((source, primary, target))
    return found


# --- 指标 / 维度草稿 ---
def _build_metrics(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], fact: str,
                   date_field: str, keys: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """数值列 -> 指标候选;单调列 -> semi_additive + last(§6.1 的重点,防时间求和)。

    事实表在前,同名数值列的指标名归属因此稳定;非事实表的指标带 source(§3.2 多事实表);
    日期维度表的数值列是日期分量(年 / 月 / 日),一律跳过。
    """
    date_dim = next((t for t in sorted(tables) if t != fact and keys.get(t) == date_field), "")
    metrics: dict[str, dict[str, Any]] = {}
    for table in ([fact] if fact else []) + sorted(t for t in tables if t != fact):
        if table == date_dim:
            continue
        for column, stat in tables[table].items():
            if _kind(stat) != "NUMBER" or stat["cardinality"] < _MIN_CARD:
                continue
            if _ID_TOKEN in _parts(column):
                continue                      # 数值代理键不是指标
            semi = stat["monotonic"] is True
            name = column if column not in metrics else f"{table}_{column}"
            metrics[name] = {"label": column, "expression": f"SUM({column})",
                             "type": "semi_additive" if semi else "additive",
                             "time_aggregation": "last" if semi else "sum",
                             "depends_on": [column],
                             **({"source": table} if table != fact else {})}
    return metrics


def _build_dimensions(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], fact: str,
                      keys: Mapping[str, str],
                      fks: list[tuple[str, str, str]]) -> dict[str, dict[str, Any]]:
    """非事实表 -> 维度条目:层级从粗到细、主键在末端;没有候选主键的表不做维度。

    层级只收低基数(≤ _LOW_CARD)的文本列,剔除主键、展示名列(带 NAME 词元)、候选外键列(通往
    另一张维度的链接)与带强日期词元的列(时间轴,不是层级);数值列不做层级:基数低不代表是层级。
    """
    dimensions: dict[str, dict[str, Any]] = {}
    fk_columns = {(source, column) for source, column, _target in fks}
    for table in sorted(tables):
        primary = keys.get(table, "")
        if table == fact or not primary:
            continue
        columns = tables[table]
        names = [c for c, s in columns.items() if c != primary and _kind(s) != "NUMBER"
                 and _NAME_TOKEN in _parts(c) and s["cardinality"] >= _MIN_LEVEL_CARD]
        name_column = min(names, key=lambda c: (-columns[c]["cardinality"], c)) if names else ""
        levels = sorted([c for c, s in columns.items()
                         if c not in (primary, name_column) and (table, c) not in fk_columns
                         and _kind(s) not in ("NUMBER", "DATE")
                         and not _parts(c) & _STRONG_DATE_TOKENS
                         and _MIN_LEVEL_CARD <= s["cardinality"] <= _LOW_CARD],
                        key=lambda c: (columns[c]["cardinality"], c))
        parts = table.split("_")
        name = ("_".join(parts[1:]) if len(parts) > 1 and parts[0].upper() in _TABLE_PREFIXES
                else table)
        entry: dict[str, Any] = {"label": name, "type": "table", "table": table, "key": primary}
        if name_column:
            entry["name_column"] = name_column
        entry["hierarchy"] = [*levels, primary]
        dimensions[name if name not in dimensions else table] = entry
    return dimensions


# --- _todo:把判据、近似口径与缺口写给人工(与 YAML 注释里的标记同形) ---
def _build_notes(rows: Mapping[str, int], fact: str, date_info: tuple[str, str],
                 keys: Mapping[str, str], fks: list[tuple[str, str, str]],
                 metrics: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """草稿的疑点清单:推断判据、近似口径与缺口。参数多是有意的——这里就是汇总出口。"""
    date_field, date_reason = date_info
    notes = [f"{_TODO}: 本草稿由表结构统计推断,只覆盖机器能确定的部分——业务口径、日历、"
             f"caveats、分解声明都不在其中,合入 semantic.yaml 前逐条人工确认。"]
    if fact:
        head = (f"{_TODO}: 事实表候选 {fact}(约 {rows[fact]} 行;行数由「去重值 / 唯一性」反推),"
                f"判据是「行数最多」——若真实事实表是另一张,指标的 source 都要改。")
        notes.append(head + (f" 日期字段候选 {date_field}({date_reason}),粒度也须人工确认。"
                             if date_field else
                             " 事实表里没找到像时间轴的列,date_field 留空,请人工指定。"))
    else:
        notes.append(f"{_TODO}: 没能从统计里反推行数,事实表候选为空——请人工指定 fact_table。")
    semi = [f"{metric.get('source') or fact}.{metric['label']}"
            for _name, metric in sorted(metrics.items()) if metric["type"] == "semi_additive"]
    if semi:
        notes.append(f"{_TODO}: 半可加候选(取值随时间单调不减):{', '.join(semi)}——已按 "
                     f"semi_additive + time_aggregation: last 起草(窗口内取期末值,不是逐期"
                     f"求和,§1.3);若其实是累计流量,应改回 additive + sum。")
    notes.append(f"{_TODO}: 指标候选只覆盖「数值列 -> SUM」一种口径;COUNT / 去重类与 "
                 f"derived / ratio 型的表达式推断不出来,必须人工补写。"
                 + ("" if metrics else " 本次没推断出任何指标候选,请人工指定。"))
    found = ", ".join(f"{table}.{column}" for table, column in sorted(keys.items()) if column)
    notes.append(f"{_TODO}: 候选主键(唯一性 = 1.0):{found or '无'};事实表 {fact or '(空)'} 的"
                 f"候选主键 {keys.get(fact) or '未找到(没有唯一列,可能是多行一实体)'}。")
    if fks:
        pairs = "; ".join(f"{source}.{c} → {target}.{c}" for source, c, target in fks)
        notes.append(f"{_TODO}: 候选外键(近似口径:同名 + 目标列是候选主键 + 源列基数 ≤ 目标列"
                     f"基数):{pairs}——只是**可能**包含,精确包含率须现场查数据。")
    else:
        notes.append(f"{_TODO}: 没找到候选外键(同名列且基数不超目标键列),连接键请人工指定。")
    notes.append(f"{_TODO}: 维度层级只收低基数(≤ {_LOW_CARD})文本列,已剔除主键、展示名列与"
                 f"外键列;日期分量类数值列(年 / 月 / 日)既不做层级也不做指标。")
    empty = sorted(table for table, count in rows.items() if count <= 0)
    if empty:
        notes.append(f"{_TODO}: 表 {', '.join(empty)} 反推不出行数(可能是空表),已跳过其推断。")
    return notes
