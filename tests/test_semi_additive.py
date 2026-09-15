"""半可加指标(time_aggregation: last)的查询语义(§5.6 / §1.3 的静默错误防线)。

用 tmp_path 造一份最小的订阅快照事实表 + 临时语义层,验证:
    - 整窗标量 = 窗口内**最后有数据日**的值,不是求和(求和是 §1.3 的 3 倍错误)
    - 分组聚合 = 每个分组各自的末日值(QUALIFY 按日期倒序取组内第 1 行)
    - slice_rows(contribute 的取数) = 每切片每窗口的末日值
    - daily_series 保持每日值(异常检测的输入,日粒度天然可加)
    - 非 last 的 time_aggregation 抛 SemanticError(宁败不静默算错)
    - 同表可加指标仍按求和(分派不误伤普通指标)
"""

from __future__ import annotations

import duckdb
import pytest
import yaml

from attribution.engine import AttributionEngine
from attribution.semantic import SemanticError

# 三组账户:tier 1 全部活到 2026-06-15 之后;tier 2 里 A2 提前流失;tier 3 晚开早关
# 每天每账户一行快照(amount = 当日 MRR 贡献)。跨窗口求和会得到 3 倍错误(§1.3),
# 末日值语义应取最后一天。
ACCOUNTS = [
    ("A1", "t1", "2026-05-01", "2026-06-15", 10.0),
    ("A2", "t1", "2026-05-01", "2026-06-15", 20.0),
    ("A3", "t2", "2026-05-01", "2026-05-20", 30.0),   # 5 月 20 日流失
    ("A4", "t2", "2026-05-01", "2026-06-15", 40.0),
    ("A5", "t3", "2026-05-10", "2026-05-31", 50.0),   # 晚开,5 月底关
]

_TEMPLATE = """schema_version: "2.0"
dataset: semi-demo
dataset_version: "0.1.0"
fact_table: balances
date_field: day
metrics:
  mrr:
    label: MRR
    expression: SUM(amount)
    type: semi_additive
    time_aggregation: {agg}
    depends_on: [amount]
  inflow:
    label: 流入
    expression: SUM(amount)
    type: additive
    depends_on: [amount]
dimensions:
  account:
    label: 账户
    table: dim_account
    key: account_id
    hierarchy: [tier, account_id]
"""


def _build_dataset(tmp_path, agg: str = "last"):
    """tmp_path 里造事实表 + 维度表 + 语义层,返回 (data_dir, semantic_path)。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with duckdb.connect() as con:
        rows = [(aid, tier, day, amt)
                for aid, tier, start, end, amt in ACCOUNTS
                for day in _days(start, end)]
        con.execute("CREATE TABLE balances AS SELECT * FROM "
                    "(VALUES " + ", ".join(f"('{a}', '{t}', '{d}', {v})"
                                           for a, t, d, v in rows)
                    + ") AS t(account_id, tier, day, amount)")
        con.execute(f"COPY balances TO '{data_dir / 'balances.parquet'}' (FORMAT PARQUET)")
        con.execute("CREATE TABLE dim_account AS SELECT * FROM "
                    "(VALUES ('A1', 't1'), ('A2', 't1'), ('A3', 't2'), ('A4', 't2'), "
                    "('A5', 't3')) AS t(account_id, tier)")
        con.execute(f"COPY dim_account TO '{data_dir / 'dim_account.parquet'}' (FORMAT PARQUET)")
    layer = tmp_path / "semantic.yaml"
    layer.write_text(_TEMPLATE.format(agg=agg), encoding="utf-8")
    return str(data_dir), str(layer)


def _days(start: str, end: str) -> list[str]:
    import datetime as dt
    origin = dt.date.fromisoformat(start)
    stop = dt.date.fromisoformat(end)
    return [(origin + dt.timedelta(days=i)).isoformat()
            for i in range((stop - origin).days + 1)]


def test_scalar_takes_last_day_value_not_sum(tmp_path) -> None:
    data_dir, semantic = _build_dataset(tmp_path)
    eng = AttributionEngine(data_dir, semantic)

    # 2026-06-01 ~ 06-15:三个活账户(A1/A2/A4),末日值 = 10+20+40 = 70;
    # 若按 sum 求和会得到 15 天的总流入(1050),是 §1.3 的静默错误
    total = eng.query_metric("mrr", [], {}, "2026-06-01", "2026-06-15")["total"]
    assert total == 70.0

    # 窗口末日恰为流失日:tier2 的 A3 在 05-20 后消失,窗口 05-01~05-20 末日值
    # = 全员(10+20+30+40+50=150,A5 于 05-10 加入);窗口 05-21~05-31 末日值
    # 不含 A3(10+20+40+50=120,A5 活到 05-31 当天)
    assert eng.query_metric("mrr", [], {}, "2026-05-01", "2026-05-20")["total"] == 150.0
    assert eng.query_metric("mrr", [], {}, "2026-05-21", "2026-05-31")["total"] == 120.0

    # 无数据的窗口:None 而不是 0(没有行不等于 0)
    assert eng.query_metric("mrr", [], {}, "2026-07-01", "2026-07-15")["total"] is None


def test_aggregate_by_tier_takes_each_tier_last_day(tmp_path) -> None:
    data_dir, semantic = _build_dataset(tmp_path)
    eng = AttributionEngine(data_dir, semantic)

    # 窗口 05-01~05-31:各 tier 末日值 = t1:30(05-31 还活着), t2:40(A3 于 05-20 流失,
    # 末日只剩 A4),t3:50(05-31 当天关,仍有行)
    rows = {r["tier"]: r["value"] for r in
            eng.query_metric("mrr", ["tier"], {}, "2026-05-01", "2026-05-31")["rows"]}
    assert rows == {"t1": 30.0, "t2": 40.0, "t3": 50.0}

    # 窗口 06-01~06-15:t3 已无数据,不出现;t1/t2 末日值 = 30/40
    rows = {r["tier"]: r["value"] for r in
            eng.query_metric("mrr", ["tier"], {}, "2026-06-01", "2026-06-15")["rows"]}
    assert rows == {"t1": 30.0, "t2": 40.0}


def test_slice_rows_and_contribute_take_last_day_per_window(tmp_path) -> None:
    data_dir, semantic = _build_dataset(tmp_path)
    eng = AttributionEngine(data_dir, semantic)

    result = eng.contribute("mrr", "account", "tier",
                            "2026-05-01", "2026-05-31", "2026-06-01", "2026-06-15")
    by_tier = {item["key"]: (item["base"], item["cmp"]) for item in result["top"]}
    # t1: 30 -> 30;t2: 40 -> 40(A3 已在两窗口前流失,末日值都是 A4);t3: 50 -> 无(单侧)
    assert by_tier == {"t1": (30.0, 30.0), "t2": (40.0, 40.0), "t3": (50.0, 0.0)}


def test_daily_series_is_per_day_values(tmp_path) -> None:
    data_dir, semantic = _build_dataset(tmp_path)
    eng = AttributionEngine(data_dir, semantic)

    series = dict(eng.source.daily_series("mrr", "2026-05-10", "2026-05-12"))
    # 05-10 起 A5 加入:30+30+40+50 = 150;之后不变
    assert series == {"2026-05-10": 150.0, "2026-05-11": 150.0, "2026-05-12": 150.0}


def test_unsupported_time_aggregation_fails_loud(tmp_path) -> None:
    data_dir, semantic = _build_dataset(tmp_path, agg="avg")
    with pytest.raises(SemanticError, match="尚未支持"):
        AttributionEngine(data_dir, semantic).query_metric(
            "mrr", [], {}, "2026-06-01", "2026-06-15")


def test_additive_metric_same_table_still_sums(tmp_path) -> None:
    data_dir, semantic = _build_dataset(tmp_path)
    eng = AttributionEngine(data_dir, semantic)

    # 分派不误伤:同表同表达式的 additive 指标仍按求和
    total = eng.query_metric("inflow", [], {}, "2026-05-10", "2026-05-12")["total"]
    assert total == pytest.approx(150.0 * 3)


def test_engine_accepts_layer_without_semi_additive_metrics(tmp_path) -> None:
    """既有行为不变:语义层不含半可加指标时,一切照旧(ecommerce 全绿即证明,这里补一层)。"""
    data_dir, semantic = _build_dataset(tmp_path)
    layer = yaml.safe_load(open(semantic, encoding="utf-8"))
    layer["metrics"]["mrr"]["type"] = "additive"      # 临时降级为可加
    layer["metrics"]["mrr"].pop("time_aggregation")
    path = tmp_path / "semantic2.yaml"
    path.write_text(yaml.safe_dump(layer, allow_unicode=True), encoding="utf-8")
    eng = AttributionEngine(data_dir, str(path))
    assert eng.query_metric("mrr", [], {}, "2026-05-10", "2026-05-12")["total"] == 450.0


def test_metric_source_reads_from_its_own_fact_table(tmp_path) -> None:
    """多事实表(§3.2 的 source 声明):指标取自哪张表由语义层说了算,引擎按 metric.source 取数。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with duckdb.connect() as con:
        con.execute("CREATE TABLE balances AS SELECT * FROM (VALUES "
                    "('A1', '2026-05-02', 9.0)) AS t(account_id, day, amount)")
        con.execute(f"COPY balances TO '{data_dir / 'balances.parquet'}' (FORMAT PARQUET)")
        con.execute("CREATE TABLE movements AS SELECT * FROM (VALUES "
                    "('A1', '2026-05-01', 5.0), ('A1', '2026-05-02', 3.0), "
                    "('A2', '2026-05-01', 7.0)) AS t(account_id, day, delta)")
        con.execute(f"COPY movements TO '{data_dir / 'movements.parquet'}' (FORMAT PARQUET)")
        con.execute("CREATE TABLE dim_account AS SELECT * FROM "
                    "(VALUES ('A1'), ('A2')) AS t(account_id)")
        con.execute(f"COPY dim_account TO '{data_dir / 'dim_account.parquet'}' (FORMAT PARQUET)")
    layer = tmp_path / "semantic.yaml"
    layer.write_text(yaml.safe_dump({
        "schema_version": "2.0", "dataset": "multi-fact-demo", "dataset_version": "0.1.0",
        "fact_table": "balances", "date_field": "day",
        "metrics": {
            "delta_sum": {"label": "变动和", "expression": "SUM(delta)",
                          "type": "additive", "source": "movements", "depends_on": ["delta"]},
        },
        "dimensions": {
            "account": {"label": "账户", "table": "dim_account", "key": "account_id",
                        "hierarchy": ["account_id"]},
        },
    }, allow_unicode=True), encoding="utf-8")
    # delta_sum 必须来自 movements:若引擎无视 source 去查 balances(只有 amount 列),
    # 编译期就会报「列 delta 不存在」;sum 结果也能证明取数来源(9 是 balances 的 amount)
    eng = AttributionEngine(str(data_dir), str(layer))
    total = eng.query_metric("delta_sum", [], {}, "2026-05-01", "2026-05-31")["total"]
    assert total == 15.0     # 5 + 3 + 7,来自 movements 而不是 balances(amount=9)
    rows = eng.query_metric("delta_sum", ["account_id"], {}, "2026-05-01", "2026-05-31")["rows"]
    assert {r["account_id"]: r["value"] for r in rows} == {"A1": 8.0, "A2": 7.0}
