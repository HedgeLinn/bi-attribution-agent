"""派生维度(kind: derived,如同期群)的查询语义(§5.5 的 user-journey 前置能力)。

派生维度没有维度表:层级字段直接住在**事实表**上。引擎必须不 join、
字段引用落在事实表别名——否则会生成「join 空表」的非法 SQL(实测曾崩)。
"""

from __future__ import annotations

import duckdb
import yaml

from attribution.engine import AttributionEngine

_LAYER = {
    "schema_version": "2.0", "dataset": "derived-demo", "dataset_version": "0.1.0",
    "fact_table": "events", "date_field": "day",
    "metrics": {
        "events_count": {"label": "事件数", "expression": "COUNT(*)", "type": "additive",
                         "depends_on": []},
        "users": {"label": "活跃用户", "expression": "COUNT(DISTINCT user_id)",
                  "type": "additive", "depends_on": ["user_id"]},
    },
    "dimensions": {
        "cohort": {"label": "同期群", "type": "derived",
                   "hierarchy": ["signup_date"]},
        "date": {"label": "日期", "table": "events", "key": "day", "hierarchy": ["day"]},
    },
}


def _build(tmp_path) -> AttributionEngine:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with duckdb.connect() as con:
        con.execute("CREATE TABLE events AS SELECT * FROM (VALUES "
                    "('u1', '2026-05-01', '2026-05-01'), "
                    "('u2', '2026-05-02', '2026-05-01'), "
                    "('u1', '2026-05-03', '2026-05-01'), "
                    "('u3', '2026-05-03', '2026-05-03')) AS t(user_id, day, signup_date)")
        con.execute(f"COPY events TO '{data_dir / 'events.parquet'}' (FORMAT PARQUET)")
    layer = tmp_path / "semantic.yaml"
    layer.write_text(yaml.safe_dump(_LAYER, allow_unicode=True), encoding="utf-8")
    return AttributionEngine(str(data_dir), str(layer))


def test_derived_dimension_queries_without_join(tmp_path) -> None:
    eng = _build(tmp_path)

    rows = {r["signup_date"]: r["value"] for r in
            eng.query_metric("events_count", ["signup_date"], {},
                             "2026-05-01", "2026-05-31")["rows"]}
    assert rows == {"2026-05-01": 3, "2026-05-03": 1}   # 按事实表上的字段分组


def test_derived_dimension_filter_works(tmp_path) -> None:
    eng = _build(tmp_path)

    total = eng.query_metric("users", [], {"signup_date": "2026-05-01"},
                             "2026-05-01", "2026-05-31")["total"]
    assert total == 2.0   # u1 + u2


def test_derived_dimension_contribute_slices(tmp_path) -> None:
    """contribute 走 slice_rows:派生维度层级在事实表上,FULL JOIN 配对同样成立。"""
    eng = _build(tmp_path)

    result = eng.contribute("events_count", "cohort", "signup_date",
                            "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-03")
    by_key = {item["key"]: (item["base"], item["cmp"]) for item in result["top"]}
    assert by_key == {"2026-05-01": (2.0, 1.0), "2026-05-03": (0.0, 1.0)}
