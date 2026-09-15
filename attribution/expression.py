"""表达式编译器:把语义层里的 SQL 风格表达式编译成 DuckDB 可执行的聚合 SQL。

契约:docs/REUSE_DESIGN.md §3.1

设计约束:
    - **纯函数**:不碰数据、不碰 IO(列名由调用方传入),可独立单测
    - **不硬编码任何数据集词汇**:指标名 / 列名一律来自参数
    - **fail fast**:编译失败抛 ExpressionError,绝不返回半成品

本模块不认识任何具体数据集,也不认识 AttributionEngine。
"""

import re
from collections.abc import Collection, Mapping

__all__ = ["ExpressionError", "compile_expression", "referenced_identifiers"]

# 已知 SQL 关键字与内置函数(大小写无关)。命中者原样保留,不参与指标 / 列名替换。
# compile_expression 与 referenced_identifiers **共用这一份清单**,两处判断必须一致。
# 只收录聚合表达式里常见的词,不追求覆盖 DuckDB 全部函数;需要新函数时在此增量补充。
_KNOWN_SQL_WORDS: frozenset[str] = frozenset(
    {
        # 聚合函数
        "SUM", "COUNT", "AVG", "MEAN", "MIN", "MAX", "MEDIAN", "MODE",
        "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE", "VAR_POP", "VAR_SAMP",
        "ANY_VALUE", "FIRST", "LAST", "ARRAY_AGG", "LIST", "STRING_AGG",
        "GROUP_CONCAT", "BOOL_AND", "BOOL_OR", "BIT_AND", "BIT_OR",
        "APPROX_COUNT_DISTINCT",
        # 条件 / 空值 / 逻辑
        "NULLIF", "IFNULL", "COALESCE", "IF", "IIF", "GREATEST", "LEAST",
        "CASE", "WHEN", "THEN", "ELSE", "END", "NULL", "TRUE", "FALSE",
        "AND", "OR", "NOT", "IS", "IN", "BETWEEN", "LIKE", "ILIKE",
        "EXISTS", "ANY", "ALL", "SOME", "DISTINCT", "AS", "WHERE", "FROM",
        # 窗口 / 排序
        "FILTER", "OVER", "PARTITION", "BY", "ORDER", "ASC", "DESC", "NULLS",
        "ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE", "LAG", "LEAD",
        "FIRST_VALUE", "LAST_VALUE",
        # 数值函数
        "ABS", "ROUND", "FLOOR", "CEIL", "CEILING", "TRUNC", "SIGN", "SQRT",
        "POW", "POWER", "EXP", "LN", "LOG", "LOG2", "LOG10", "MOD", "RANDOM",
        # 字符串函数
        "CONCAT", "CONCAT_WS", "LOWER", "UPPER", "LENGTH", "SUBSTR",
        "SUBSTRING", "TRIM", "LTRIM", "RTRIM", "REPLACE", "REGEXP_REPLACE",
        "REGEXP_MATCHES", "SPLIT_PART", "LPAD", "RPAD", "POSITION", "STRPOS",
        # 类型转换(CAST(x AS <类型>) 里的类型名同样是裸标识符)
        "CAST", "TRY_CAST", "TRY", "BOOLEAN", "TINYINT", "SMALLINT", "INTEGER",
        "BIGINT", "HUGEINT", "FLOAT", "REAL", "DOUBLE", "DECIMAL", "NUMERIC",
        "VARCHAR", "TEXT", "STRING", "BLOB", "UUID", "JSON",
        # 日期时间
        "EXTRACT", "INTERVAL", "NOW", "TODAY",
    }
)

# 单引号字符串字面量(含 '' 转义)。整体跳过,避免字面量里的词被误判成标识符。
_STRING_LITERAL = r"'(?:[^']|'')*'"
_STRING_LITERAL_PATTERN = re.compile(_STRING_LITERAL)

# 词法单元:字符串字面量 | 裸标识符。其余字符(运算符 / 数字 / 空白)由 re.sub 原样保留。
_TOKEN_PATTERN = re.compile(rf"{_STRING_LITERAL}|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)")

# 标识符之后(允许空白)紧跟左括号 —— 即函数调用位置
_CALL_PATTERN = re.compile(r"\s*\(")


class ExpressionError(Exception):
    """表达式编译失败(语法错误、未知引用等)。"""


def compile_expression(
    expr: str,
    symbols: Mapping[str, str],
    columns: Collection[str],
    alias: str = "o",
) -> str:
    """把语义层 expression 编译成可内联进 SQL 的 DuckDB 表达式。

    参数:
        expr:    语义层 expression 原文,如 'SUM(amount)'、'gmv / NULLIF(orders_count, 0)'
        symbols: 指标名 -> 已编译 SQL 的映射(用于展开 derived 指标之间的引用)
        columns: 事实表可用列名集合(用于判断裸标识符是否为列)
        alias:   事实表别名;列引用编译为 f'{alias}.{col}'

    返回:
        可直接内联进 DuckDB SQL 的表达式字符串。

    规则:
        1. **替换顺序**:先展开 symbols,再处理列名。
           symbols 内**长名优先**,避免 'gmv' 误伤 'gmv_per_user'。
        2. **裸标识符**命中 columns -> 编译为 f'{alias}.{col}'。
        3. **SQL 关键字与函数**(SUM / COUNT / DISTINCT / NULLIF / IFNULL 等)原样保留。
        4. **除零由表达式自己负责**:约定 derived 表达式显式写 `NULLIF(分母, 0)`,
           编译器不做隐式包装(保持 expression 是「SQL 风格」,不发明 DSL)。
        5. 既不在 symbols、不在 columns、也不是已知 SQL 关键字的标识符 -> 抛 ExpressionError。

    异常:
        ExpressionError: 语法非法,或引用了无法解析的标识符。
    """
    if not expr or not expr.strip():
        raise ExpressionError("表达式为空")
    _check_syntax(expr)

    known_columns = frozenset(columns)

    def replace(match: re.Match[str]) -> str:
        return _resolve(match, expr, symbols, known_columns, alias)

    # 逐「词」替换而不是对整串做字符串替换:标识符按最长连续片段切开,
    # 长名因此天然优先——短名只会整体命中它自己,绝不会命中长名内部的一段;
    # 单次遍历同时完成「先 symbols、再 columns」,展开结果不会被二次替换。
    return _TOKEN_PATTERN.sub(replace, expr)


def referenced_identifiers(expr: str) -> list[str]:
    """抽出表达式真正**引用**的标识符(去重,保持首次出现顺序)。

    供语义层做「depends_on 与表达式实际引用是否一致」的校验用。
    只做词法抽取,不判断引用的标识符是否合法——合法性由 compile_expression 负责。

    抽取规则(两条都要满足才算引用):
        1. 排除**函数名**——标识符后紧跟 `(` 的,视为函数名
        2. 排除 **SQL 关键字**——与 compile_expression 共用同一份关键字清单

    例:
        referenced_identifiers('SUM(amount)')                  -> ['amount']
        referenced_identifiers('gmv / NULLIF(orders_count, 0)') -> ['gmv', 'orders_count']
        referenced_identifiers('COUNT(DISTINCT order_id)')      -> ['order_id']
    """
    refs: list[str] = []
    seen: set[str] = set()
    for match in _TOKEN_PATTERN.finditer(expr):
        name = match.group("ident")
        if name is None or name in seen:
            continue                              # 字符串字面量 / 已收录过
        if _is_call(expr, match.end()):
            continue                              # 函数名:不算引用
        if name.upper() in _KNOWN_SQL_WORDS:
            continue                              # SQL 关键字:不算引用
        seen.add(name)
        refs.append(name)
    return refs


# ----------------------------------------------------------------------
# 内部辅助(不对外暴露)
# ----------------------------------------------------------------------
def _is_call(expr: str, end: int) -> bool:
    """标识符结束位置 end 之后(允许空白)是否紧跟 '(' —— 是则为函数名。"""
    return _CALL_PATTERN.match(expr, end) is not None


def _check_syntax(expr: str) -> None:
    """轻量语法校验:字符串字面量闭合 + 括号配对(不解析语法树)。

    目的是 fail fast —— 不把「编译器已经知道不对」的表达式留给 DuckDB 去报错。
    """
    without_literals = _STRING_LITERAL_PATTERN.sub("", expr)
    if "'" in without_literals:
        raise ExpressionError(f"表达式存在未闭合的字符串字面量:{expr!r}")
    depth = 0
    for char in without_literals:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ExpressionError(f"表达式的括号不匹配:{expr!r}")
    if depth:
        raise ExpressionError(f"表达式的括号不匹配:{expr!r}")


def _resolve(
    match: re.Match[str],
    expr: str,
    symbols: Mapping[str, str],
    columns: frozenset[str],
    alias: str,
) -> str:
    """把一个词法单元解析成最终 SQL 片段;无法解析则抛 ExpressionError。

    判定顺序:
        ① 字符串字面量 / SQL 函数名 -> 原样保留
        ② 命中 symbols -> 展开为已编译的指标 SQL
        ③ 命中 columns -> 加事实表别名
        ④ 其余 SQL 关键字 -> 原样保留
        ⑤ 都不命中 -> ExpressionError(报错信息点名该标识符)
    """
    name = match.group("ident")
    if name is None:
        return match.group(0)
    if _is_call(expr, match.end()) and name.upper() in _KNOWN_SQL_WORDS:
        return name
    if name in symbols:
        return symbols[name]
    if name in columns:
        return f"{alias}.{name}"
    if name.upper() in _KNOWN_SQL_WORDS:
        return name
    raise ExpressionError(
        f"表达式引用了无法解析的标识符 {name!r}:"
        f"既不是 symbols 里的指标,也不是可用列,也不是已知 SQL 关键字;表达式:{expr!r}"
    )
