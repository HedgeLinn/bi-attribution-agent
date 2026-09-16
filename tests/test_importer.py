"""通用数据导入(harness/importer.py)的验收:CSV / Excel -> **待确认包** -> 可分析。

对应 docs/PLAN.md 的「通用数据导入」:把任意 CSV / Excel 导进来,系统自动识别
「数值列 -> 指标、低基数文本列 -> 维度」,生成语义层草稿,让数据立即可分析。

**导入只写到 `datasets/.pending/<id>/`**:自动推断的地图合法但可以错(见
harness/importer.py 的模块 docstring 与 docs/REUSE_DESIGN.md §6.2),所以它必须过
人工确认(harness/import_confirm.py)才落位成正式数据集。本文件的集成用例一律走
`_import_and_commit`,顺带把 commit 的端到端路径(校验 + 冒烟 + 落位)也覆盖掉。

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
from harness.import_confirm import CONFIRMED_VERSION, ConfirmError, commit
from harness.importer import (
    _DATASET_VERSION,
    _FACT_TABLE,
    _MAX_ROWS,
    _PARQUET_NAME,
    DRAFT_NAME,
    ImporterError,
    import_upload,
    pending_dir,
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


def _import_and_commit(name: str, data: bytes, datasets_dir: Path,
                       answers: dict | None = None) -> dict:
    """导入 + 用空答案走一遍 commit,返回 import 的结果 dict(包已落在 datasets_dir/<id>/)。

    空答案是刻意的:它等价于「向导每一步都跳过」,于是这些用例既验证了导入与草稿,
    又免费覆盖了 commit 的落位路径 —— 而不是各测一半。
    """
    result = import_upload(name, data, datasets_dir=datasets_dir)
    assert result["ok"] is True, result
    assert commit(result["id"], answers or {}, datasets_dir=datasets_dir)["ok"] is True
    return result


def _draft_of(result: dict, datasets_dir: Path) -> dict:
    """读回待确认包里的草稿语义层。"""
    path = pending_dir(datasets_dir) / result["id"] / DRAFT_NAME
    return yaml.safe_load(path.read_text(encoding="utf-8"))


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
    assert frame is not None
    assert pick_date_column(frame) == ""


# ---------------------------------------------------------------------------
# 草稿:身份三处写死 + 指标 key 错开列名(还没过人工闸门的形态)
# ---------------------------------------------------------------------------
def test_import_writes_loadable_draft_into_pending(tmp_path) -> None:
    """导入只产草稿,落在 .pending/ 里,且草稿本身就是一份合法地图的骨架。"""
    result = import_upload("sales.csv", _SALES_CSV.encode("utf-8"), datasets_dir=tmp_path)
    assert result["ok"] is True

    root = pending_dir(tmp_path) / result["id"]
    assert (root / DRAFT_NAME).is_file()
    assert (root / "data" / _PARQUET_NAME).is_file()   # 坑 4:事实表 = parquet stem
    assert not (tmp_path / result["id"]).exists()      # 还没落位

    draft = _draft_of(result, tmp_path)
    assert draft["dataset"] == result["id"]                  # dataset == 目录名
    assert draft["dataset_version"] == _DATASET_VERSION      # 不能空 / 不能是 draft
    assert draft["fact_table"] == _FACT_TABLE
    assert "_todo" not in draft                              # 不留草稿标记
    # 指标 key 与列名错开(引擎 load 会把同名当依赖成环,见 import_draft.unique_metric_names)
    assert "amount" not in draft["metrics"]
    assert "amount_sum" in draft["metrics"]
    assert set(result["metrics"]) == {"amount_sum", "quantity_sum"}
    assert "region" in result["dimensions"] and "category" in result["dimensions"]


# ---------------------------------------------------------------------------
# 集成:CSV 导入 -> 确认落位 -> 引擎可分析
# ---------------------------------------------------------------------------
def test_csv_import_creates_analyzable_package(tmp_path) -> None:
    result = _import_and_commit("sales.csv", _SALES_CSV.encode("utf-8"), tmp_path)
    package = tmp_path / result["id"]

    assert (package / "semantic.yaml").is_file()
    assert (package / "dataset.yaml").is_file()
    assert (package / "data" / _PARQUET_NAME).is_file()
    assert not (pending_dir(tmp_path) / result["id"]).exists()   # 暂存区已清场

    semantic = yaml.safe_load((package / "semantic.yaml").read_text(encoding="utf-8"))
    assert semantic["dataset"] == result["id"]
    assert semantic["dataset_version"] == CONFIRMED_VERSION   # 过了人工闸门的版本号
    assert semantic["fact_table"] == _FACT_TABLE

    engine = AttributionEngine(str(package / "data"), str(package / "semantic.yaml"))
    total = engine.query_metric("amount_sum", [], {}, "2026-06-01", "2026-06-30")["total"]
    assert total == 620.0


def test_csv_import_derives_regional_dimension(tmp_path) -> None:
    """低基数文本列 -> 可下钻的 derived 维度(单表场景的关键,引擎不 join)。"""
    result = _import_and_commit("sales.csv", _SALES_CSV.encode("utf-8"), tmp_path)
    package = tmp_path / result["id"]
    semantic = yaml.safe_load((package / "semantic.yaml").read_text(encoding="utf-8"))
    region = semantic["dimensions"]["region"]
    assert region["type"] == "derived" and region["hierarchy"] == ["region"]

    # 引擎真的能按它分组下钻
    engine = AttributionEngine(str(package / "data"), str(package / "semantic.yaml"))
    rows = engine.query_metric("amount_sum", ["region"], {}, "2026-06-01", "2026-06-30")["rows"]
    assert {row["region"]: row["value"] for row in rows} == {"华东": 420, "华北": 200}


def test_csv_import_skips_numeric_id_column(tmp_path) -> None:
    """数值代理键(order_id)不该被识别成指标(命中 profile 的 _ID_TOKEN 跳过规则)。

    顺带走一遍「没有维度时手工指认可下钻列」:这张表全是数值列,自动推断出 0 个维度,
    光靠草稿过不了闸门(见下一条用例),向导第 ① 步的回答是它唯一的出路。
    """
    result = _import_and_commit("ids.csv", _ID_COLUMN_CSV.encode("utf-8"), tmp_path,
                                answers={"dimensions": ["order_id"]})
    assert "order_id" not in result["metrics"]
    assert "amount_sum" in result["metrics"]


def test_commit_requires_at_least_one_dimension(tmp_path) -> None:
    """一张没有任何低基数文本列的表,自动推断出 0 个维度 —— 引擎拒收,commit 必须拦住。

    这不是边角料,而是「为什么必须有人工确认」的直接体现:自动推断只把基数 2~50 的
    文本列升格为维度,一张全数值列的表就一个维度都没有,而引擎的 check_reachability
    会判它「没有任何维度可用于下钻」。拦截 = 不落位,且待确认包原样保留供改答案重试。
    """
    result = import_upload("ids.csv", _ID_COLUMN_CSV.encode("utf-8"), datasets_dir=tmp_path)
    assert result["ok"] is True
    assert result["dimensions"] == []
    with pytest.raises(ConfirmError) as err:
        commit(result["id"], {}, datasets_dir=tmp_path)
    assert "维度" in str(err.value)
    assert not (tmp_path / result["id"]).exists()            # 拦住 = 不落位
    assert (pending_dir(tmp_path) / result["id"]).is_dir()   # 待确认包还在,可改答案重试


def test_csv_string_dates_become_real_dates(tmp_path) -> None:
    """字符串日期列落盘后必须转成真实 DATE(引擎日期过滤才能按时间序比较)。"""
    result = _import_and_commit("sales.csv", _SALES_CSV.encode("utf-8"), tmp_path)
    package = tmp_path / result["id"]
    with duckdb.connect() as con:
        rows = con.execute(
            "SELECT typeof(date) FROM read_parquet(?)",
            [str(package / "data" / _PARQUET_NAME)],
        ).fetchall()
    assert rows and rows[0][0] == "DATE"


def test_xlsx_import_works(tmp_path) -> None:
    """Excel 导入:read_excel + 同样的识别、落包、确认链路。"""
    bytes_io = io.BytesIO()
    read_upload("sales.csv", _SALES_CSV.encode("utf-8")).to_excel(bytes_io, index=False)
    result = _import_and_commit("sales.xlsx", bytes_io.getvalue(), tmp_path)
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
    # 半截目录必须清场:否则它会一直混在待确认列表里,直到超时清理
    assert not list(pending_dir(tmp_path).glob("*"))


def test_unique_ids_do_not_collide(tmp_path) -> None:
    """同一文件名两次导入 -> 两个独立待确认包、互不覆盖(时间戳 + _2 后缀区分)。"""
    first = import_upload("sales.csv", _SALES_CSV.encode("utf-8"), datasets_dir=tmp_path)
    second = import_upload("sales.csv", _SALES_CSV.encode("utf-8"), datasets_dir=tmp_path)
    assert first["ok"] and second["ok"]
    assert first["id"] != second["id"]
    base = pending_dir(tmp_path)
    assert (base / first["id"]).is_dir() and (base / second["id"]).is_dir()


def test_id_is_sanitized_and_prefixed(tmp_path) -> None:
    """中文 / 空格等文件名的 id 只留字母数字下划线连字符,且带 upload_ 前缀。"""
    result = _import_and_commit("六月 销售(数据).csv", _SALES_CSV.encode("utf-8"), tmp_path)
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