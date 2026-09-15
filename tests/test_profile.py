"""语义收集 A 档的测试:统计探测(scripts/profile_data.py)+ 草稿推断(attribution/profile.py)。

三条线,缺一条这套验证就不成立:
    ① gather_stats 的每个统计量在自造 parquet 上逐值可验(基数 / 唯一性 / 空值率 / 单调性);
    ② suggest_semantic_draft 的候选识别(主键 / 外键 / 指标 / 半可加 / 层级)与边界
       ——空表、坏文件、非法形状都不许抛错(草稿宁缺毋滥,但也不能崩);
    ③ main 的退出码与落盘(用法错误 2;--dataset 默认写回数据集包内)。

测试数据一律落在 tmp_path 里,不读写 datasets/,不联网。单调性探测是本模块最容易被
「看起来对」骗到的一处,所以正例(不回落)与反例(回落)都要跑到,还要一条「没有日期列
就不探测」的通道。
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import yaml

from attribution.profile import suggest_semantic_draft
from scripts.profile_data import gather_stats, main

# 事实表:amount / quantity 基数 3、note 有 4/6 空值;order_id 唯一;date_id 是字符串日期
# amount 按日的合计 300 / 300 / 100 是**回落**的流量型列(单调性探测不该命中)
_ORDER_DDL = ("order_id VARCHAR, date_id VARCHAR, store_id VARCHAR, "
              "amount BIGINT, quantity BIGINT, note VARCHAR")
_ORDER_VALUES = """
    ('o1', '2026-06-01', 'S1', 100, 2, NULL),
    ('o2', '2026-06-01', 'S2', 200, 1, 'x'),
    ('o3', '2026-06-02', 'S1', 100, 3, NULL),
    ('o4', '2026-06-02', 'S2', 200, 1, NULL),
    ('o5', '2026-06-03', 'S1', 50, 1, 'y'),
    ('o6', '2026-06-03', 'S2', 50, 2, NULL)
"""
# 维度表:store_id 唯一、store_name 是展示名、region / city 是低基数层级
_STORE_DDL = "store_id VARCHAR, store_name VARCHAR, region VARCHAR, city VARCHAR"
_STORE_VALUES = "('S1', '一店', '北区', '北京'), ('S2', '二店', '北区', '北京'), ('S3', '三店', '南区', '上海')"
# 日期维度表:year / month 是日期分量(数值列,但不是指标)
_DATE_DDL = "date_id VARCHAR, year BIGINT, month BIGINT"
_DATE_VALUES = "('2026-06-01', 2026, 6), ('2026-06-02', 2026, 6), ('2026-06-03', 2026, 6)"
# 余额型列(bal 不回落 = 半可加候选)与流量型列(flow 回落 1/3 = 不命中)
_BAL_DDL = "d DATE, bal BIGINT, flow BIGINT"
_BAL_VALUES = ("(DATE '2026-01-01', 10, 5), (DATE '2026-01-02', 12, 7), "
               "(DATE '2026-01-03', 15, 2), (DATE '2026-01-04', 15, 5)")
# 没有任何日期列的普通表(不该被探测单调性)
_PLAIN_DDL = "sku VARCHAR, price BIGINT, cost BIGINT"
_PLAIN_VALUES = "('k1', 10, 6), ('k2', 20, 9), ('k3', 30, 12)"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """数据集 id 还认环境变量:测试里清掉,结果只取决于参数。"""
    monkeypatch.delenv("BI_DATASET", raising=False)


def _parquet(path: Path, ddl: str, values: str = "") -> None:
    """用 DuckDB 直接写 parquet(values 是 VALUES 子句;留空 = 0 行表)。"""
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t ({ddl})")
    if values:
        con.execute(f"INSERT INTO t VALUES {values}")
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    con.close()


@pytest.fixture()
def data_dir(tmp_path) -> Path:
    """四张表的合成数据目录:事实表 + 维度表 + 余额表 + 无日期表。"""
    directory = tmp_path / "data"
    directory.mkdir()
    _parquet(directory / "orders.parquet", _ORDER_DDL, _ORDER_VALUES)
    _parquet(directory / "dim_store.parquet", _STORE_DDL, _STORE_VALUES)
    _parquet(directory / "dim_date.parquet", _DATE_DDL, _DATE_VALUES)
    _parquet(directory / "bal_daily.parquet", _BAL_DDL, _BAL_VALUES)
    _parquet(directory / "catalog.parquet", _PLAIN_DDL, _PLAIN_VALUES)
    return directory


@pytest.fixture()
def ragged_dir(tmp_path) -> Path:
    """烂数据目录:一张 0 行表 + 一个后缀是 parquet 的坏文件。"""
    directory = tmp_path / "ragged"
    directory.mkdir()
    _parquet(directory / "empty.parquet", "a VARCHAR, b BIGINT")
    (directory / "broken.parquet").write_bytes(b"this is not parquet at all")
    return directory


# ---------------------------------------------------------------------------
# ① gather_stats:统计量逐值可验
# ---------------------------------------------------------------------------
def test_gather_stats_measures(data_dir):
    stats = gather_stats(str(data_dir))
    assert stats["orders"]["order_id"]["uniqueness"] == 1.0            # 唯一 -> 候选主键
    assert stats["orders"]["amount"]["cardinality"] == 3
    assert stats["orders"]["store_id"]["uniqueness"] == pytest.approx(1 / 3)
    assert stats["orders"]["note"]["null_rate"] == pytest.approx(4 / 6, abs=1e-6)
    assert stats["orders"]["amount"]["type"] == "BIGINT"
    assert stats["dim_store"]["store_id"]["cardinality"] == 3


def test_gather_stats_monotonic(data_dir):
    stats = gather_stats(str(data_dir))
    assert stats["bal_daily"]["bal"]["monotonic"] is True              # 10→12→15→15 不回落
    assert stats["bal_daily"]["flow"]["monotonic"] is False            # 5→7→2→5 回落 1/3
    assert stats["orders"]["amount"]["monotonic"] is False             # 非累计型列不误报


def test_gather_stats_skips_probe_without_date_column(data_dir):
    stats = gather_stats(str(data_dir))
    assert stats["catalog"]["price"]["monotonic"] is False
    assert stats["catalog"]["cost"]["monotonic"] is False


def test_gather_stats_survives_empty_and_broken_files(ragged_dir):
    stats = gather_stats(str(ragged_dir))
    assert stats["empty"]["b"] == {"cardinality": 0, "null_rate": 0.0, "uniqueness": 0.0,
                                   "type": "BIGINT", "monotonic": False}
    assert stats["broken"] == {}                     # 读不成 parquet:整表缺失,不抛错


def test_gather_stats_rejects_missing_directory(tmp_path):
    with pytest.raises(ValueError):
        gather_stats(str(tmp_path / "nope"))


def test_gather_stats_falls_back_to_per_column(data_dir, monkeypatch):
    """受控注入:批量聚合坏掉时必须逐列退让,统计照样齐(不是整表判死)。"""
    import scripts.profile_data as profiler
    monkeypatch.setattr(profiler, "_batch_sql", lambda path, names: "SELECT * FROM no_such_table")
    stats = gather_stats(str(data_dir))
    assert stats["orders"]["order_id"]["uniqueness"] == 1.0
    assert stats["orders"]["amount"]["cardinality"] == 3


# ---------------------------------------------------------------------------
# ② suggest_semantic_draft:候选识别与边界
# ---------------------------------------------------------------------------
def test_draft_recognizes_candidates(data_dir):
    draft = suggest_semantic_draft(gather_stats(str(data_dir)))
    assert draft["fact_table"] == "orders"           # 行数最多
    assert draft["date_field"] == "date_id"          # 字符串日期:判据要写明这一点
    assert draft["metrics"]["amount"]["expression"] == "SUM(amount)"
    assert draft["metrics"]["amount"]["type"] == "additive"
    assert draft["metrics"]["bal"]["type"] == "semi_additive"          # 半可加候选
    assert draft["metrics"]["bal"]["time_aggregation"] == "last"
    assert draft["metrics"]["bal"]["source"] == "bal_daily"            # 非事实表要带 source
    store = draft["dimensions"]["store"]
    assert store["key"] == "store_id" and store["name_column"] == "store_name"
    assert store["hierarchy"][-1] == "store_id" and {"region", "city"} <= set(store["hierarchy"])


def test_draft_splits_date_dimension_from_metrics(data_dir):
    draft = suggest_semantic_draft(gather_stats(str(data_dir)))
    # 日期维度表的数值列是日期分量:既不做层级也不做指标(否则 year/month 会变成指标)
    assert "year" not in draft["metrics"] and "month" not in draft["metrics"]
    assert draft["dimensions"]["date"]["hierarchy"] == ["date_id"]


def test_draft_notes_keys_and_fks(data_dir):
    notes = "\n".join(suggest_semantic_draft(gather_stats(str(data_dir)))["_todo"])
    assert "dim_date.date_id" in notes and "dim_store.store_id" in notes      # 候选主键
    assert "orders 的候选主键 order_id" in notes                              # 事实表有唯一列
    assert "orders.store_id → dim_store.store_id" in notes                    # 候选外键
    assert "半可加候选" in notes and "bal_daily.bal" in notes                 # 半可加只报指标


def test_draft_prefers_typed_date_column(tmp_path):
    directory = tmp_path / "typed"
    directory.mkdir()
    _parquet(directory / "t.parquet", "d DATE, v BIGINT",
             "(DATE '2026-01-01', 1), (DATE '2026-01-02', 2)")
    draft = suggest_semantic_draft(gather_stats(str(directory)))
    assert draft["fact_table"] == "t" and draft["date_field"] == "d"
    assert any("类型本身是日期" in note for note in draft["_todo"])


def test_draft_rejects_fk_wider_than_target():
    """源列基数 > 目标键列基数 -> 一定包含不下:宁可漏报也不许报成候选外键。"""
    stats = {"f": {"city_id": {"type": "VARCHAR", "cardinality": 5, "null_rate": 0.0,
                              "uniqueness": 0.5}},
             "d": {"city_id": {"type": "VARCHAR", "cardinality": 3, "null_rate": 0.0,
                              "uniqueness": 1.0}}}
    draft = suggest_semantic_draft(stats)
    assert draft["dimensions"]["d"]["key"] == "city_id"
    assert any("没找到候选外键" in note for note in draft["_todo"])


def test_draft_survives_empty_and_broken(ragged_dir):
    draft = suggest_semantic_draft(gather_stats(str(ragged_dir)))
    assert draft["fact_table"] == "" and draft["metrics"] == {} and draft["dimensions"] == {}
    assert draft["_todo"]                            # 推断不出来也要把疑点写出来


@pytest.mark.parametrize("bad", [None, [], "x", 3, {"t": None}, {"t": {"c": "not-a-mapping"}},
                                 {"t": {"c": {"type": "BIGINT", "cardinality": "??"}}}])
def test_draft_never_raises_on_illegal_stats(bad):
    draft = suggest_semantic_draft(bad)
    assert draft["metrics"] == {} and draft["_todo"]
    assert set(draft) == {"schema_version", "dataset", "dataset_version", "fact_table",
                          "date_field", "metrics", "dimensions", "_todo"}


# ---------------------------------------------------------------------------
# ③ main:退出码与落盘
# ---------------------------------------------------------------------------
def test_main_writes_draft(data_dir, tmp_path):
    out = tmp_path / "draft.yaml"
    assert main(["--data-dir", str(data_dir), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# TODO: 人工确认")       # 草稿标记必须在文件里
    draft = yaml.safe_load(text)
    assert draft["fact_table"] == "orders" and draft["date_field"] == "date_id"
    assert draft["metrics"]["bal"]["type"] == "semi_additive"
    assert draft["dataset"] == ""                    # --data-dir:没有数据集名可填


def test_main_dataset_writes_into_package(data_dir, tmp_path):
    root = tmp_path / "pkg-demo"
    (root / "data").mkdir(parents=True)
    for source in data_dir.glob("*.parquet"):
        (root / "data" / source.name).write_bytes(source.read_bytes())
    (root / "dataset.yaml").write_text("id: pkg-demo\ntitle: 临时数据集\n", encoding="utf-8")
    assert main(["--dataset", str(root)]) == 0       # 不给 --out:写回数据集包内
    draft = yaml.safe_load((root / "semantic.draft.yaml").read_text(encoding="utf-8"))
    assert draft["dataset"] == "pkg-demo" and draft["fact_table"] == "orders"


def test_main_usage_errors(tmp_path, capsys):
    missing = str(tmp_path / "nope")
    for argv in ([],                                                     # 两个都不给
                 ["--data-dir", str(tmp_path), "--dataset", "x"],        # 两个都给
                 ["--dataset", "no-such-dataset-at-all"],                # 数据集不存在
                 ["--data-dir", missing],                                # 目录不存在
                 ["--data-dir", str(tmp_path), "--out", str(tmp_path / "sub" / "x.yaml")]):
        assert main(argv) == 2, argv
        assert "错误:" in capsys.readouterr().err


def test_main_help_is_not_an_error(capsys):
    assert main(["--help"]) == 0                     # argparse 的 0 原样透传
    assert "semantic.draft.yaml" in capsys.readouterr().out
