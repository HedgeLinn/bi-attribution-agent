"""pytest 公共设施:从所有语义层 YAML 与数据 parquet 收集「数据集词汇」。

供机械防回退测试使用(docs/REUSE_DESIGN.md §3.6⑤):
要知道哪些词**属于具体数据集**,才能断言引擎代码里一个都不能出现。

收集的类别:
    1. 指标名(metrics 的键)与维度名(dimensions 的键)
    2. 维度的 key / name_column / hierarchy 字段
    3. 表名:fact_table + 各维度的 table
    4. **真实列名**:语义层声明的表在 data/*.parquet 里的全部列(含表达式没引用到的列——
       只从 expression / depends_on 反推,会漏掉 day / week / base_price / tier 这类列)
    5. 指标表达式与 depends_on 引用的标识符(§3.5 的引用口径)
    6. decompositions 的 target / entity_dimension / factors(§4.1)
    7. time.calendar 的日历名(如 promos)
    8. label 里的 **ASCII 展示名**(如 GMV)
    9. 实体**取值**:维度表 key / name_column 列与事实表维度键列的真实值
       (每列最小的 30 个,如 STORE_S0001 / 上海徐家汇旗舰店;纯数字成员无害)

关于 label:ASCII 展示名纳入——引擎里写 `"GMV"` 这种按展示名特判的数据集逻辑,与写
`amount` 是同一类回退。中文展示名(如「门店」)**不纳入**:它们不可能以标识符形态出现在
引擎里,纳入只会在引擎的中文提示文案上产生假阳性,收益为零、误报成本不为零。

设计约束:
    - 词汇**全部**从语义层 YAML 与数据 parquet 读出,不硬编码任何数据集词汇(否则测试自我循环)
    - 同时兼容当前与将来两种布局:
        semantic/semantic.yaml            (当前)
        datasets/<name>/semantic.yaml     (M2 数据集包化之后)
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import duckdb
import yaml

# 项目根目录:tests/conftest.py -> 上一级
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 语义层扫描路径:两种布局都扫,迁移时词汇表不会突然变空
SEMANTIC_PATTERNS: tuple[str, ...] = (
    "semantic/semantic.yaml",
    "datasets/*/semantic.yaml",
)

# 数据目录名与文件后缀:真实列名只在数据集包的 data/ 下读得到
DATA_DIRNAME = "data"
DATA_SUFFIX = ".parquet"

# SQL 语法词汇:属于 SQL 语言本身,不是数据集词汇,解析 expression 时排除
SQL_KEYWORDS: frozenset[str] = frozenset({
    "SUM", "COUNT", "DISTINCT", "AVG", "MIN", "MAX", "NULLIF", "IFNULL",
    "COALESCE", "CAST", "AS", "AND", "OR", "NOT", "NULL", "CASE", "WHEN",
    "THEN", "ELSE", "END", "FILTER", "OVER", "PARTITION", "BY",
})

# 标识符词法:字母/下划线开头,下划线算词内字符(故 date_field 不会切出 date)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 纯 ASCII 标识符形态:用于筛掉中文展示名
_ASCII_WORD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 实体值收集:每列最多取最小的 N 个 DISTINCT 值(ORDER BY 保证稳定,可复现)
_VALUE_LIMIT = 30


def discover_semantic_files() -> list[Path]:
    """按当前与将来两种布局收集语义层文件,去重后排序返回。"""
    found: list[Path] = []
    for pattern in SEMANTIC_PATTERNS:
        found.extend(PROJECT_ROOT.glob(pattern))
    return sorted(set(found))


def collect_vocabulary_from_all_datasets() -> set[str]:
    """从所有语义层 YAML(及其声明的数据表)收集数据集词汇。

    返回:
        词汇集合。找不到任何语义层文件时抛 FileNotFoundError——
        词汇表为空会让防回退测试变成永远通过的假绿,必须显式失败。
    """
    files = discover_semantic_files()
    if not files:
        raise FileNotFoundError(
            f"未找到任何语义层文件,已扫描 {list(SEMANTIC_PATTERNS)}(根目录 {PROJECT_ROOT})"
        )
    columns_by_table = _columns_for_declared_tables(files)
    vocabulary: set[str] = set()
    for path in files:
        vocabulary |= _vocabulary_from_semantic_file(path, columns_by_table)
    vocabulary |= _entity_value_vocabulary(files)
    return vocabulary


def _load_yaml(path: Path) -> dict[str, Any]:
    """读 YAML,顶层不是 mapping 时返回空 dict。"""
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """YAML 节点统一成 mapping,yaml.safe_load 返回 None 时退化。"""
    return value if isinstance(value, Mapping) else {}


def _strings_in(value: Any) -> set[str]:
    """把可能是 str / list[str] 的字段统一成字符串集合。"""
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {item for item in value if isinstance(item, str)}
    return set()


def _identifiers_in_expression(expr: str) -> set[str]:
    """抽出 expression 引用的标识符:排除函数名(后跟括号)与 SQL 关键字。"""
    names: set[str] = set()
    for match in _WORD_RE.finditer(expr):
        name = match.group()
        if name.upper() in SQL_KEYWORDS:
            continue
        if expr[match.end():].lstrip().startswith("("):  # 函数名
            continue
        names.add(name)
    return names


def _candidate_data_dirs(files: list[Path]) -> list[Path]:
    """数据目录候选:每个语义层同级的 `data/`,外加历史布局的项目根 `data/`。

    M2 数据集包化后,parquet 随语义层一起搬进了 `datasets/<id>/data/`;
    保留根 `data/` 作兜底,是为了让迁移前后的词汇表都能收全(迁移期不能有空窗)。
    """
    dirs = [path.parent / DATA_DIRNAME for path in files]
    dirs.append(PROJECT_ROOT / DATA_DIRNAME)
    unique: list[Path] = []
    for one in dirs:
        if one not in unique:
            unique.append(one)
    return unique


def _columns_for_declared_tables(files: list[Path]) -> dict[str, set[str]]:
    """按语义层声明的表名,读出真实列名(在语义层同级的数据集包里找 parquet)。

    单个表的 parquet 缺失时跳过(表名本身仍会被收集);一个都读不到时显式失败——
    词汇表缩水同样会让防回退测试漏检,不能静默降级。
    """
    tables: set[str] = set()
    for path in files:
        tables |= _declared_table_names(_load_yaml(path))

    data_dirs = _candidate_data_dirs(files)
    columns: dict[str, set[str]] = {}
    con = duckdb.connect()
    try:
        for table in sorted(tables):
            name = f"{table}{DATA_SUFFIX}"
            parquet = next((d / name for d in data_dirs if (d / name).is_file()), None)
            if parquet is None:
                continue
            rows = con.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [parquet.as_posix()]
            ).fetchall()
            columns[table] = {str(row[0]) for row in rows}
    finally:
        con.close()

    if not columns:
        searched = ", ".join(str(d) for d in data_dirs)
        raise FileNotFoundError(
            f"未从任何数据目录读到语义层声明的表 {sorted(tables)}——已查找:{searched}。"
            "真实列名缺失会让防回退测试漏检,请先运行 scripts/generate_data.py"
        )
    return columns


def _declared_table_names(data: Mapping[str, Any]) -> set[str]:
    """语义层声明的表名:事实表 + 各维度的 table。"""
    tables = _strings_in(data.get("fact_table"))
    for dimension in _as_mapping(data.get("dimensions")).values():
        if isinstance(dimension, Mapping):
            tables |= _strings_in(dimension.get("table"))
    return tables


def _vocabulary_from_semantic_file(
    path: Path, columns_by_table: Mapping[str, set[str]]
) -> set[str]:
    """单个语义层文件的词汇(类别清单见模块 docstring)。"""
    data = _load_yaml(path)
    metrics = _as_mapping(data.get("metrics"))
    dimensions = _as_mapping(data.get("dimensions"))

    vocabulary: set[str] = set(metrics) | set(dimensions)
    vocabulary |= _dimension_vocabulary(dimensions)
    vocabulary |= _declared_table_names(data)
    vocabulary |= _real_column_vocabulary(data, columns_by_table)
    vocabulary |= _fact_column_vocabulary(data, metrics)
    vocabulary |= _decomposition_vocabulary(data)
    vocabulary |= _calendar_vocabulary(data)
    vocabulary |= _label_vocabulary(metrics, dimensions)
    return {word for word in vocabulary if isinstance(word, str) and word}


def _dimension_vocabulary(dimensions: Mapping[str, Any]) -> set[str]:
    """维度自带词汇:key(切片主键)、name_column(展示名)、hierarchy(下钻层级)。"""
    vocabulary: set[str] = set()
    for dimension in dimensions.values():
        if not isinstance(dimension, Mapping):
            continue
        vocabulary |= _strings_in(dimension.get("key"))
        vocabulary |= _strings_in(dimension.get("name_column"))
        vocabulary |= _strings_in(dimension.get("hierarchy"))
    return vocabulary


def _real_column_vocabulary(
    data: Mapping[str, Any], columns_by_table: Mapping[str, set[str]]
) -> set[str]:
    """语义层声明的表在数据里的真实列名(含表达式没引用到的列)。"""
    vocabulary: set[str] = set()
    for table in _declared_table_names(data):
        vocabulary |= columns_by_table.get(table, set())
    return vocabulary


def _decomposition_vocabulary(data: Mapping[str, Any]) -> set[str]:
    """分解声明引用的名字:target(被归因对象)、entity_dimension、factors(§4.1)。"""
    vocabulary: set[str] = set()
    entries = data.get("decompositions")
    if not isinstance(entries, (list, tuple)):
        return vocabulary
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        vocabulary |= _strings_in(entry.get("target"))
        vocabulary |= _strings_in(entry.get("entity_dimension"))
        vocabulary |= _strings_in(entry.get("factors"))
    return vocabulary


def _calendar_vocabulary(data: Mapping[str, Any]) -> set[str]:
    """时间日历名(如 promos):日历是数据集特有的口径知识,引擎不该写死。"""
    calendar = _as_mapping(_as_mapping(data.get("time")).get("calendar"))
    return {name for name in calendar if isinstance(name, str) and name}


def _label_vocabulary(
    metrics: Mapping[str, Any], dimensions: Mapping[str, Any]
) -> set[str]:
    """指标/维度的 ASCII 展示名(如 GMV);中文展示名不收(理由见模块 docstring)。"""
    labels: set[str] = set()
    for section in (metrics, dimensions):
        for entity in section.values():
            if isinstance(entity, Mapping):
                labels |= _strings_in(entity.get("label"))
    return {label for label in labels if _ASCII_WORD_RE.match(label)}


def _fact_column_vocabulary(
    data: Mapping[str, Any], metrics: Mapping[str, Any]
) -> set[str]:
    """事实表列名:date_field + 指标表达式/依赖里除指标名外的标识符。"""
    referenced: set[str] = _strings_in(data.get("date_field"))
    for metric in metrics.values():
        if not isinstance(metric, Mapping):
            continue
        referenced |= _strings_in(metric.get("depends_on"))
        expression = metric.get("expression")
        if isinstance(expression, str):
            referenced |= _identifiers_in_expression(expression)
    return referenced - set(metrics) - SQL_KEYWORDS


def _entity_value_vocabulary(files: list[Path]) -> set[str]:
    """维度表 key / name_column / hierarchy 列与事实表维度键列的**取值**(每列最小的 N 个)。

    防回退测试抓得住列名,但抓不住值——引擎写死 `STORE_S0001` / `上海徐家汇旗舰店`
    与写死 `store_id` 是同一类回退,值同样属于具体数据集。值以 str() 形态入库:
    纯数字成员不构成任何词法匹配,天然无害;含中文的成员与扫描侧的整串匹配配合
    (扫描侧见到含中文的字面量整串入库,两边同一口径)。
    parquet / 列缺失时静默跳过:列名收集已把「读不到数据」的失败守住,
    值收集是增强项,不在同一件事上重复设卡。
    """
    vocabulary: set[str] = set()
    data_dirs = _candidate_data_dirs(files)
    con = duckdb.connect()
    try:
        for path in files:
            data = _load_yaml(path)
            dimensions = _as_mapping(data.get("dimensions"))
            fact_tables = _strings_in(data.get("fact_table"))
            for dimension in dimensions.values():
                if not isinstance(dimension, Mapping):
                    continue
                table = dimension.get("table")
                if not isinstance(table, str):
                    continue
                for column in (dimension.get("key"), dimension.get("name_column")):
                    if isinstance(column, str):
                        vocabulary |= _column_value_sample(con, data_dirs, table, column)
                for column in dimension.get("hierarchy") or ():
                    if isinstance(column, str):    # 层级列(region / city / …)的值也是数据集知识
                        vocabulary |= _column_value_sample(con, data_dirs, table, column)
                key = dimension.get("key")
                if isinstance(key, str):        # 事实表的维度键列与维度 key 同列名
                    for fact in fact_tables:
                        vocabulary |= _column_value_sample(con, data_dirs, fact, key)
    finally:
        con.close()
    return {word for word in vocabulary if word}


def _column_value_sample(
    con: duckdb.DuckDBPyConnection, data_dirs: list[Path], table: str, column: str
) -> set[str]:
    """一列最小的 _VALUE_LIMIT 个 DISTINCT 值(str 形态);表/列读不到时返回空集。"""
    filename = f"{table}{DATA_SUFFIX}"
    parquet = next((d / filename for d in data_dirs if (d / filename).is_file()), None)
    if parquet is None:
        return set()
    try:
        rows = con.execute(
            f"SELECT DISTINCT {column} FROM read_parquet(?)"
            f" ORDER BY {column} LIMIT {_VALUE_LIMIT}",
            [parquet.as_posix()],
        ).fetchall()
    except duckdb.Error:            # 该表没有这一列:值收集是增强项,跳过即可
        return set()
    return {str(row[0]) for row in rows if row[0] is not None}
