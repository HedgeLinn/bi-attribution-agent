"""attribution.expression 单测:表达式编译器是纯函数,以下用例全部离线可跑。

覆盖:正常聚合 / 多指标嵌套引用 / 除零(NULLIF 原样保留,不做隐式包装)/ 长名优先 /
函数名与关键字不被误判为引用 / 未知标识符报错且点名 / 空表达式 / 大小写敏感。
"""

import pytest

from attribution.expression import (
    ExpressionError,
    compile_expression,
    referenced_identifiers,
)

# 模拟 semantic.py 传给编译器的「已编译指标 SQL」,真实值由引擎递归展开得到
SYMBOLS = {
    "gmv": "SUM(o.amount)",
    "orders_count": "COUNT(DISTINCT o.order_id)",
    "gmv_per_user": "SUM(o.amount) / NULLIF(COUNT(DISTINCT o.user_id), 0)",
}

# 模拟 DuckDB DESCRIBE 运行时探测出的事实表列名
COLUMNS = {
    "amount",
    "quantity",
    "order_id",
    "refund",
    "discount",
    "channel",
    "user_id",
}


# ----------------------------------------------------------------------
# compile_expression:正常聚合
# ----------------------------------------------------------------------
def test_compile_bare_column_gets_alias_prefix():
    assert compile_expression("SUM(amount)", symbols={}, columns=COLUMNS) == "SUM(o.amount)"


def test_compile_count_distinct_keeps_sql_syntax():
    assert (
        compile_expression("COUNT(DISTINCT order_id)", symbols={}, columns=COLUMNS)
        == "COUNT(DISTINCT o.order_id)"
    )


def test_compile_uses_given_alias():
    assert (
        compile_expression("SUM(amount)", symbols={}, columns=COLUMNS, alias="f")
        == "SUM(f.amount)"
    )


def test_compile_keeps_operators_and_literals_verbatim():
    expr = "SUM(CASE WHEN channel = 'paid' THEN amount ELSE 0 END)"
    assert compile_expression(expr, symbols={}, columns=COLUMNS) == (
        "SUM(CASE WHEN o.channel = 'paid' THEN o.amount ELSE 0 END)"
    )


# ----------------------------------------------------------------------
# compile_expression:多指标嵌套引用
# ----------------------------------------------------------------------
def test_compile_expands_several_symbols():
    """derived 表达式 = 若干指标引用 + SQL 函数,两侧都要正确。"""
    assert compile_expression("gmv / NULLIF(orders_count, 0)", SYMBOLS, COLUMNS) == (
        "SUM(o.amount) / NULLIF(COUNT(DISTINCT o.order_id), 0)"
    )


def test_compile_expands_symbol_next_to_bare_column():
    """同一个表达式里既有指标引用也有裸列(如 SUM(refund) / gmv)。"""
    assert compile_expression("SUM(refund) / gmv", SYMBOLS, COLUMNS) == (
        "SUM(o.refund) / SUM(o.amount)"
    )


def test_compile_expands_repeated_symbol_at_every_occurrence():
    assert compile_expression("gmv / gmv", SYMBOLS, COLUMNS) == "SUM(o.amount) / SUM(o.amount)"


def test_compile_symbol_wins_over_same_named_column():
    """命中 symbols 的标识符不再当列名处理,避免被二次加别名。"""
    assert compile_expression("gmv", {"gmv": "SUM(o.amount)"}, {"gmv"}, "o") == "SUM(o.amount)"


# ----------------------------------------------------------------------
# compile_expression:除零由表达式自己负责
# ----------------------------------------------------------------------
def test_compile_keeps_explicit_nullif():
    assert "NULLIF(o.amount, 0)" in compile_expression(
        "SUM(refund) / NULLIF(amount, 0)", symbols={}, columns=COLUMNS
    )


def test_compile_does_not_wrap_division_implicitly():
    """编译器只做替换:没写 NULLIF 就不许偷偷补上。"""
    out = compile_expression("gmv / orders_count", SYMBOLS, COLUMNS)
    assert out == "SUM(o.amount) / COUNT(DISTINCT o.order_id)"
    assert "NULLIF" not in out


# ----------------------------------------------------------------------
# compile_expression:长名优先
# ----------------------------------------------------------------------
def test_compile_prefers_longer_symbol_name_after_short_one():
    assert compile_expression("gmv + gmv_per_user", SYMBOLS, COLUMNS) == (
        "SUM(o.amount) + SUM(o.amount) / NULLIF(COUNT(DISTINCT o.user_id), 0)"
    )


def test_compile_prefers_longer_symbol_name_before_short_one():
    assert compile_expression("gmv_per_user + gmv", SYMBOLS, COLUMNS) == (
        "SUM(o.amount) / NULLIF(COUNT(DISTINCT o.user_id), 0) + SUM(o.amount)"
    )


def test_compile_does_not_patch_long_identifier_from_short_name():
    """短名只整体命中自己:未知的近似长名必须报错,而不是被替换出一段残缺 SQL。"""
    with pytest.raises(ExpressionError) as excinfo:
        compile_expression("gmv_per_order", SYMBOLS, COLUMNS)
    assert "gmv_per_order" in str(excinfo.value)


# ----------------------------------------------------------------------
# compile_expression:未知标识符 / 空表达式 / 大小写
# ----------------------------------------------------------------------
@pytest.mark.parametrize("expr", ["revenue", "SUM(revenue)", "gmv / revenue"])
def test_compile_unknown_identifier_is_named_in_error(expr):
    with pytest.raises(ExpressionError) as excinfo:
        compile_expression(expr, SYMBOLS, COLUMNS)
    assert "revenue" in str(excinfo.value)


def test_compile_unknown_function_is_rejected():
    with pytest.raises(ExpressionError) as excinfo:
        compile_expression("NOT_A_FUNCTION(amount)", symbols={}, columns=COLUMNS)
    assert "NOT_A_FUNCTION" in str(excinfo.value)


@pytest.mark.parametrize("expr", ["", "   ", "\t\n"])
def test_compile_empty_expression_raises(expr):
    with pytest.raises(ExpressionError):
        compile_expression(expr, SYMBOLS, COLUMNS)


def test_compile_identifier_matching_is_case_sensitive():
    """列名 / 指标名区分大小写:AMOUNT 不是 amount。"""
    with pytest.raises(ExpressionError) as excinfo:
        compile_expression("AMOUNT", symbols={}, columns=COLUMNS)
    assert "AMOUNT" in str(excinfo.value)


def test_compile_symbol_matching_is_case_sensitive():
    with pytest.raises(ExpressionError) as excinfo:
        compile_expression("GMV", SYMBOLS, COLUMNS)
    assert "GMV" in str(excinfo.value)


def test_compile_sql_words_are_case_insensitive():
    """SQL 关键字 / 函数名按 SQL 语义大小写不敏感,小写写法同样原样保留。"""
    assert compile_expression("sum(amount)", symbols={}, columns=COLUMNS) == "sum(o.amount)"


@pytest.mark.parametrize(
    "expr",
    ["SUM(amount", "SUM(amount))", "SUM(CASE WHEN channel = 'paid THEN amount END)"],
)
def test_compile_illegal_syntax_raises(expr):
    with pytest.raises(ExpressionError):
        compile_expression(expr, symbols={}, columns=COLUMNS)


# ----------------------------------------------------------------------
# referenced_identifiers:docstring 的三个示例
# ----------------------------------------------------------------------
def test_referenced_identifiers_example_aggregate():
    assert referenced_identifiers("SUM(amount)") == ["amount"]


def test_referenced_identifiers_example_division():
    assert referenced_identifiers("gmv / NULLIF(orders_count, 0)") == ["gmv", "orders_count"]


def test_referenced_identifiers_example_count_distinct():
    assert referenced_identifiers("COUNT(DISTINCT order_id)") == ["order_id"]


# ----------------------------------------------------------------------
# referenced_identifiers:函数名 / 关键字 / 去重 / 纯词法
# ----------------------------------------------------------------------
def test_referenced_identifiers_skips_functions_and_keywords():
    assert referenced_identifiers("IFNULL(SUM(refund), 0) / NULLIF(SUM(amount), 0)") == [
        "refund",
        "amount",
    ]


def test_referenced_identifiers_skips_case_keywords():
    assert referenced_identifiers("SUM(CASE WHEN channel = 'paid' THEN amount ELSE 0 END)") == [
        "channel",
        "amount",
    ]


def test_referenced_identifiers_dedupes_and_keeps_first_order():
    assert referenced_identifiers("gmv + amount * gmv - amount") == ["gmv", "amount"]


def test_referenced_identifiers_ignores_string_literal_content():
    refs = referenced_identifiers("SUM(IF(channel = 'paid_media', amount, 0))")
    assert refs == ["channel", "amount"]


def test_referenced_identifiers_is_lexical_only():
    """只做词法抽取:未知标识符原样返回,合法性由 compile_expression 负责。"""
    assert referenced_identifiers("revenue + SUM(profit)") == ["revenue", "profit"]


@pytest.mark.parametrize("expr", ["", "   "])
def test_referenced_identifiers_empty_expression_returns_empty_list(expr):
    assert referenced_identifiers(expr) == []
