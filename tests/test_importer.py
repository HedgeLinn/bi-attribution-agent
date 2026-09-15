"""通用数据导入(harness/importer.py)的验收:CSV / Excel -> 数据集包 -> 可分析。

对应 docs/PLAN.md 的「通用数据导入」:把任意 CSV / Excel 导进来,系统自动识别
「数值列 -> 指标、低基数文本列 -> 维度」,生成语义层,让数据立即可分析。

**验证主线**:落包后语义层必须能被 AttributionEngine 真正用起来——construct 会做
validate + check_reachability,query_metric 能取数,这两步过了才叫「可分析」;
只断言「文件写出来了 / YAML 形状对」是不够的(那套曾经就是"看起来对")。

导入产生的数据集 id 带时间戳,测试一律用 tmp_path 隔离,绝不写 datasets/。
"""

from __future__ import annotations

import io
from pathlib import Path

import duckdb
import pytest
import yaml

from attribution.engine import AttributionEngine
from harness.importer import (
    _DATASET_VERSION,
    _FACT_TABLE,
    _MAX_ROWS,
    _PARQUET_NAME,
    ImporterError,
    import_upload,
    pick_date_column,
    read_upload,
)

# 一份典型销售单:日期 + 区域 / 品类(低基数文本维度)+ 订单号 / 金额 / 数量(指标)
_SALES_CSV = """date,region,category,order_id,amount,quantity
2026-06-01,华东,数码,o1,100,2
2026-06-01,华东,服饰,o2,200,1
2026-06-02,华北,数码,o3,150,3
2026-06-02,华北,服饰,o4,50,1
2026-06-03,华东,数码,o5,120,2
"""
# 无日期列的裸表(无 override -> no_date_column;给了 override -> 成功)
_NO_DATE_CSV = """region,amount
华东,100
华北,200
华南,50
"""
# 数值 ID 列:order_id 是数值但是代理键,不该被识别成指标
_ID_COLUMN_CSV = """date,order_id,amount
2026-06-01,101,100
2026-06-02,102,200
2026-06-03,103,150
"""
# 空文件(无列):直接判失败
_EMPTY_CSV = ""


# ---------------------------------------------------------------------------
# 单元:文件读取与日期列识别
# ---------------------------------------------------------------------------
def test_read_upload_csv_makes_string_columns() -> None:
    frame = read_upload("sales.csv", _SALES_CSV.encode("utf-8"))
    assert list(frame.columns) == ["date", "region", "category", "order_id",
                                   "amount", "quantity"]
    assert all(isinstance(c, str) for c in frame.columns)
    assert len(frame) == 5


def test_read_upload_rejects_unsupported_type() -> None:
    with pytest.raises(ImporterError):
        read_upload("data.json", b"{}")


def test_read_upload_rejects_bad_content() -> None:
    with pytest.raises(ImporterError):
        read_upload("broken.csv", b"\x00\xffnot a real csv")


def test_pick_date_column_finds_string_date() -> None:
    frame = read_upload("sales.csv", _SALES_CSV.encode("utf-8"))
    assert pick_date_column(frame) == "date"


def test_pick_date_column_returns_empty_when_no_date() -> None:
    frame = read_upload("n.csv", _NO_DATE_CSV.encode("utf-8"))
    assert pick_date_column(frame) == ""


# ---------------------------------------------------------------------------
# 集成:CSV 导入 -> 数据集包 -> 引擎可分析
# ---------------------------------------------------------------------------
def test_csv_import_creates_analyzable_package(tmp_path) -> None:
    result = import_upload("sales.csv", _SALES_CSV.encode("utf-8"), datasets_dir=tmp_path)
    assert result["ok"] is True
    package = tmp_path / result["id"]

    assert (package / "semantic.yaml").is_file()
    assert (package / "dataset.yaml").is_file()
    assert (package / "data" / _PARQUET_NAME).is_file()   # 坑 4:事实表 = parquet stem

    semantic = yaml.safe_load((package / "semantic.yaml").read_text(encoding="utf-8"))
    assert semantic["dataset"] == result["id"]          # 坑 2:dataset == 目录名
    assert semantic["dataset_version"] == _DATASET_VERSION   # 坑 1:不能空 / draft
    assert semantic["fact_table"] == _FACT_TABLE
    assert "_todo" not in semantic                       # 不留草稿标记
    # 指标 key 与列名错开(引擎 load 会把同名当依赖成环,见 importer._unique_metric_names)
    assert "amount" not in semantic["metrics"]
    assert "amount_sum" in semantic["metrics"]
    # 识别出的指标(数值列)与维度(低基数文本列)都要在语义层里
    assert set(result["metrics"]) == {"amount_sum", "quantity_sum"}
    assert "region" in result["dimensions"] and "category" in result["dimensions"]

    engine = AttributionEngine(str(package / "data"), str(package / "semantic.yaml"))
    total = engine.query_metric("amount_sum", [], {}, "2026-06-01", "2026-06-30")["total"]
    assert total == 620.0


def test_csv_import_derives_regional_dimension(tmp_path) -> None:
    """低基数文本列 -> 可下钻的 derived 维度(单表场景的关键,引擎不 join)。"""
    result = import_upload("sales.csv", _SALES_CSV.encode("utf-8"), datasets_dir=tmp_path)
    package = tmp_path / result["id"]
    semantic = yaml.safe_load((package / "semantic.yaml").read_text(encoding="utf-8"))
    region = semantic["dimensions"]["region"]
    assert region["type"] == "derived" and region["hierarchy"] == ["region"]

    # 引擎真的能按它分组下钻
    engine = AttributionEngine(str(package / "data"), str(package / "semantic.yaml"))
    rows = engine.query_metric("amount_sum", ["region"], {}, "2026-06-01", "2026-06-30")["rows"]
    assert {row["region"]: row["value"] for row in rows} == {"华东": 420, "华北": 200}


def test_csv_import_skips_numeric_id_column(tmp_path) -> None:
    """数值代理键(order_id)不该被识别成指标(命中 profile 的 _ID_TOKEN 跳过规则)。"""
    result = import_upload("ids.csv", _ID_COLUMN_CSV.encode("utf-8"), datasets_dir=tmp_path)
    assert result["ok"] is True
    assert "order_id" not in result["metrics"]
    assert "amount_sum" in result["metrics"]


def test_csv_string_dates_become_real_dates(tmp_path) -> None:
    """字符串日期列落盘后必须转成真实 DATE(引擎日期过滤才能按时间序比较)。"""
    result = import_upload("sales.csv", _SALES_CSV.encode("utf-8"), datasets_dir=tmp_path)
    package = tmp_path / result["id"]
    with duckdb.connect() as con:
        rows = con.execute(
            "SELECT typeof(date) FROM read_parquet(?)",
            [str(package / "data" / _PARQUET_NAME)],
        ).fetchall()
    assert rows and rows[0][0] == "DATE"


def test_xlsx_import_works(tmp_path) -> None:
    """Excel 导入:read_excel + 同样的识别与落包链路。"""
    bytes_io = io.BytesIO()
    read_upload("sales.csv", _SALES_CSV.encode("utf-8")).to_excel(bytes_io, index=False)
    result = import_upload("sales.xlsx", bytes_io.getvalue(), datasets_dir=tmp_path)
    assert result["ok"] is True
    engine = AttributionEngine(str(tmp_path / result["id"] / "data"),
                               str(tmp_path / result["id"] / "semantic.yaml"))
    assert engine.query_metric("amount_sum", [], {}, "2026-06-01", "2026-06-30")["total"] == 620.0


# ---------------------------------------------------------------------------
# 时间列回退:无日期列
# ---------------------------------------------------------------------------
def test_no_date_column_returns_code_and_columns(tmp_path) -> None:
    result = import_upload("nodate.csv", _NO_DATE_CSV.encode("utf-8"), datasets_dir=tmp_path)
    assert result["ok"] is False
    assert result["code"] == "no_date_column"
    assert set(result.get("columns") or []) == {"region", "amount"}


def test_no_date_column_with_override_imports(tmp_path) -> None:
    """用户挑了「看起来最像日期」的那一列(这里是文本列)也照常导入,不是硬拒绝。"""
    result = import_upload("nodate.csv", _NO_DATE_CSV.encode("utf-8"),
                           datasets_dir=tmp_path, date_field_override="region")
    assert result["ok"] is True
    assert result["date_field"] == "region"


def test_override_with_unknown_column_fails(tmp_path) -> None:
    result = import_upload("sales.csv", _SALES_CSV.encode("utf-8"), datasets_dir=tmp_path,
                           date_field_override="not_a_column")
    assert result["ok"] is False
    assert "not_a_column" in result.get("error", "")


# ---------------------------------------------------------------------------
# 失败路径与身份唯一性
# ---------------------------------------------------------------------------
def test_empty_file_fails(tmp_path) -> None:
    result = import_upload("empty.csv", _EMPTY_CSV.encode("utf-8"), datasets_dir=tmp_path)
    assert result["ok"] is False
    assert result.get("id") is None          # 失败路径不产生数据集 id / 包


def test_no_numeric_column_fails(tmp_path) -> None:
    """只有文本列没有数值列:没有指标可分析,导入应显式失败而不是写一个空包。"""
    text_only = "date,region\n2026-06-01,华东\n2026-06-02,华北\n"
    result = import_upload("textonly.csv", text_only.encode("utf-8"),
                           datasets_dir=tmp_path)
    assert result["ok"] is False
    assert "指标" in result.get("error", "")


def test_unique_ids_do_not_collide(tmp_path) -> None:
    """同一文件名两次导入 -> 两个独立数据集、互不覆盖(按时间戳 + _2 后缀区分)。"""
    first = import_upload("sales.csv", _SALES_CSV.encode("utf-8"), datasets_dir=tmp_path)
    second = import_upload("sales.csv", _SALES_CSV.encode("utf-8"), datasets_dir=tmp_path)
    assert first["ok"] and second["ok"]
    assert first["id"] != second["id"]
    assert (tmp_path / first["id"]).is_dir() and (tmp_path / second["id"]).is_dir()


def test_id_is_sanitized_and_prefixed(tmp_path) -> None:
    """中文 / 空格等文件名的 id 只留字母数字下划线连字符,且带 upload_ 前缀。"""
    result = import_upload("六月 销售(数据).csv", _SALES_CSV.encode("utf-8"),
                           datasets_dir=tmp_path)
    assert result["ok"] is True
    assert result["id"].startswith("upload_")
    assert all(ch.isalnum() or ch in "_-" for ch in result["id"])
    # 语义层 dataset 与目录名一致,目录能被数据集扫描发现
    assert yaml.safe_load((tmp_path / result["id"] / "semantic.yaml")
                          .read_text(encoding="utf-8"))["dataset"] == result["id"]


def test_large_file_is_truncated(tmp_path) -> None:
    """超限行数截断到 _MAX_ROWS:统计与引擎不被超大文件拖垮。"""
    header = "date,region,amount\n"
    body = "".join(f"2026-06-01,region{i % 3},1\n" for i in range(_MAX_ROWS + 100))
    result = import_upload("big.csv", (header + body).encode("utf-8"), datasets_dir=tmp_path)
    assert result["ok"] is True
    assert result["rows"] == _MAX_ROWS
