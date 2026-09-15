"""scripts/verify_dataset.py 的测试:期望 schema 校验、两条回验通道、退出码、报告。

三条线:① 真实数据集(ecommerce-demo)自证——埋的坑真的可观测、幅度与声明一致;
② 自造临时数据集(tmp_path)的通过 / 不通过两条路都要跑到(只测 happy path 等于没测);
③ schema 非法必须显式失败(坏期望静默跳过等于回验形同虚设)。
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from scripts.verify_dataset import (MissingExpectationsError, load_expectations, main,
                                    verify)

REPO = Path(__file__).resolve().parents[1]
DATASET = REPO / "datasets" / "ecommerce-demo"


@pytest.fixture(autouse=True)
def _cwd_repo(monkeypatch):
    """数据集 id 解析走的是相对路径(datasets/<id>):测试一律以仓库根为 CWD。"""
    monkeypatch.chdir(REPO)

# 临时数据集的语义层:只有 gmv 一个指标、store/product 两个维度(够验两条通道)
_TMP_SEMANTIC = """
schema_version: "2.0"
dataset: tmp-demo
dataset_version: "9.9.9"
fact_table: orders
date_field: date_id
metrics:
  gmv:
    label: GMV
    expression: SUM(amount)
    type: additive
    time_aggregation: sum
    depends_on: [amount]
dimensions:
  store:
    label: 门店
    table: dim_store
    key: store_id
    name_column: store_name
    hierarchy: [region, store_id]
  product:
    label: 商品
    table: dim_product
    key: product_id
    name_column: sku_name
    hierarchy: [category, product_id]
time:
  calendar:
    promos: []
"""

# 事实行:S1/P1 5 月 3×100 -> 6 月 3×40(-73.33%);S1/P2 6 月完全消失;S2 两月持平
_ORDERS = ([("o%d" % i, f"2026-05-0{i}", "S1", "P1", 100.0) for i in (1, 2, 3)]
           + [("p%d" % i, f"2026-05-0{i}", "S1", "P2", 50.0) for i in (1, 2, 3)]
           + [("q%d" % i, f"2026-05-0{i}", "S2", "P1", 200.0) for i in (1, 2, 3)]
           + [("r%d" % i, f"2026-06-0{i}", "S1", "P1", 40.0) for i in (1, 2, 3)]
           + [("s%d" % i, f"2026-06-0{i}", "S2", "P1", 200.0) for i in (1, 2, 3)])


def _parquet(path: Path, ddl: str, rows: list[tuple]) -> None:
    """用 DuckDB 直接写 parquet(不引入 pandas,测试数据一律落在 tmp_path)。"""
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t ({ddl})")
    if rows:
        con.executemany(f"INSERT INTO t VALUES ({', '.join('?' * len(rows[0]))})", rows)
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    con.close()


@pytest.fixture()
def tmp_dataset(tmp_path) -> Path:
    """造一个自包含的临时数据集包(dataset.yaml + semantic.yaml + data/*.parquet)。"""
    root = tmp_path / "tmp-demo"
    (root / "data").mkdir(parents=True)
    (root / "dataset.yaml").write_text('id: tmp-demo\ntitle: 临时数据集\nversion: "9.9.9"\n',
                                       encoding="utf-8")
    (root / "semantic.yaml").write_text(_TMP_SEMANTIC, encoding="utf-8")
    _parquet(root / "data" / "orders.parquet",
             "order_id VARCHAR, date_id VARCHAR, store_id VARCHAR, product_id VARCHAR, amount DOUBLE",
             _ORDERS)
    _parquet(root / "data" / "dim_store.parquet",
             "store_id VARCHAR, store_name VARCHAR, region VARCHAR",
             [("S1", "一店", "北区"), ("S2", "二店", "北区")])
    _parquet(root / "data" / "dim_product.parquet",
             "product_id VARCHAR, sku_name VARCHAR, category VARCHAR",
             [("P1", "商品一", "品类A"), ("P2", "商品二", "品类A")])
    return root


def _write_expectations(root: Path, body: str) -> Path:
    path = root / "expectations.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_HEAD = 'dataset_version: "9.9.9"\nexpectations:\n'   # 与临时语义层的版本一致(不触发版本警告)


def _change(check_id: str, description: str, **over) -> str:
    """一段 metric_change 期望的 YAML 文本(gmv / S1 门店 / 5 月 vs 6 月是公共项)。

    (只写差异字段,免得每条期望都抄一遍 base / cmp / filters —— 抄错抄漏反而看不出;
    每个字段名在生成时只出现一次,over 覆盖默认值而不是追加重复键。)
    """
    fields = {"metric": "gmv", "filters": "{store_id: S1}",
              "base": "[2026-05-01, 2026-05-31]", "cmp": "[2026-06-01, 2026-06-30]", **over}
    body = "".join(f"      {key}: {value}\n" for key, value in fields.items())
    return (f"  - id: {check_id}\n    description: {description}\n"
            f"    check:\n      type: metric_change\n{body}")


def _absent(check_id: str, description: str, key: str) -> str:
    """一段 slice_absent 期望的 YAML 文本(S1 门店内、6 月窗口)。"""
    return (f"  - id: {check_id}\n    description: {description}\n    check:\n"
            f"      type: slice_absent\n      metric: gmv\n      dimension: product\n"
            f"      level: product_id\n      key: {key}\n      filters: {{store_id: S1}}\n"
            f"      window: [2026-06-01, 2026-06-30]\n")


# ---------------------------------------------------------------------------
# load_expectations:缺文件 = 无法回验(抛错),坏 schema 也必须炸
# ---------------------------------------------------------------------------
def test_load_expectations_missing_file_raises(tmp_path):
    """没有 expectations.yaml -> 「0 条期望、0 条未通过」的 0/0 全绿报告,pytest.raises 挡死。"""
    with pytest.raises(MissingExpectationsError, match="无法回验"):
        load_expectations(str(tmp_path))


def test_load_expectations_explicit_empty_list_is_author_intent(tmp_path):
    """作者显式写 `expectations: []` = 明示没有期望可回验(与「缺文件」不是一回事)。"""
    _write_expectations(tmp_path, 'dataset_version: "9.9.9"\nexpectations: []\n')
    assert load_expectations(str(tmp_path)) == {"dataset_version": "9.9.9", "expectations": []}


def test_load_expectations_real_dataset():
    spec = load_expectations(str(DATASET))
    assert spec["dataset_version"] == "1.0.0"
    assert [item["id"] for item in spec["expectations"]] == [
        "store_cliff", "sku_delist", "promo_noise",
        "pure_volume_store_drop", "pure_volume_price_flat", "pure_price_store_drop",
        "pure_price_orders_flat", "volume_price_reversal_orders_up",
        "volume_price_reversal_gmv_flat", "store_mix_shift_new_store",
        "store_mix_shift_city_up", "hidden_discount_rate_up", "hidden_discount_gmv_flat"]
    store_cliff = spec["expectations"][0]["check"]
    # YAML 里的裸日期会被读成 datetime.date,归一成引擎要的 'YYYY-MM-DD' 文本
    assert store_cliff["base"] == ("2026-05-01", "2026-05-31")
    assert store_cliff["cmp"] == ("2026-06-01", "2026-06-30")
    assert store_cliff["filters"] == {"store_id": "STORE_S0001"}
    assert (store_cliff["direction"], store_cliff["min_rate"], store_cliff["max_rate"]) == (
        "down", 0.47, 0.68)
    absent = spec["expectations"][1]["check"]
    assert absent["type"] == "slice_absent" and absent["level"] == "product_id"
    assert absent["filters"] == {"store_id": "STORE_S0001"}
    assert absent["window"] == ("2026-06-01", "2026-06-30")


@pytest.mark.parametrize("body", [
    _HEAD + '  - description: 缺 id\n    check:\n      type: metric_change\n'
            '      metric: gmv\n      base: [2026-05-01, 2026-05-31]\n'
            '      cmp: [2026-06-01, 2026-06-30]\n',                          # 缺 id
    _HEAD + "  - id: x\n    description: 缺 check\n",                      # 缺 check
    _HEAD + "  - id: x\n    description: t\n    check: {type: 未知类型}\n",  # type 未知
    _HEAD + '  - id: x\n    description: t\n    check:\n      type: metric_change\n'
            '      metric: gmv\n      base: [2026-05-01]\n',               # 窗口只有一端
    _HEAD + '  - id: x\n    description: t\n    check:\n      type: metric_change\n'
            '      metric: gmv\n      cmp: [2026-06-01, 2026-06-30]\n',    # 缺 base
    _HEAD + '  - id: x\n    description: t\n    check:\n      type: metric_change\n'
            '      metric: gmv\n      base: [2026-05-01, 2026-05-31]\n'
            '      cmp: [2026-06-01, 2026-06-30]\n      direction: sideways\n',   # 方向非法
    _HEAD + '  - id: x\n    description: t\n    check:\n      type: metric_change\n'
            '      metric: gmv\n      base: [2026-05-01, 2026-05-31]\n'
            '      cmp: [2026-06-01, 2026-06-30]\n      min_rate: -1\n',          # 负阈值
    _HEAD + '  - id: x\n    description: t\n    check:\n      type: metric_change\n'
            '      metric: gmv\n      base: [2026-05-01, 2026-05-31]\n'
            '      cmp: [2026-06-01, 2026-06-30]\n      filters: {store_id: [S1]}\n',  # 过滤值非标量
    _HEAD + '  - id: x\n    description: t\n    check:\n      type: slice_absent\n'
            '      metric: gmv\n      dimension: product\n      level: product_id\n'
            '      key: P2\n',                                             # 缺 window
    "expectations: {}\n",                                                  # 顶层类型错
    "- 只是列表\n",                                                         # 顶层不是映射
    "id: x\n",                                                             # 连 expectations 都没有
])
def test_load_expectations_bad_schema_raises(tmp_path, body):
    _write_expectations(tmp_path, body)
    with pytest.raises(ValueError):
        load_expectations(str(tmp_path))


def test_load_expectations_duplicate_id_raises(tmp_path):
    _write_expectations(tmp_path, _HEAD + _change("x", "a") + _change("x", "b"))
    with pytest.raises(ValueError, match="重复 id"):
        load_expectations(str(tmp_path))


# ---------------------------------------------------------------------------
# verify:真实数据集自证 + 临时数据集的通过/不通过两条路
# ---------------------------------------------------------------------------
def test_verify_real_dataset_all_pass():
    """自证:埋的坑真的可观测、幅度与声明一致(回验独立于造数脚本)。"""
    report = verify(str(DATASET))
    assert set(report) == {"dataset", "dataset_version", "total", "passed", "results"}
    assert (report["dataset"], report["dataset_version"]) == ("ecommerce-demo", "1.0.0")
    assert (report["total"], report["passed"]) == (13, 13)
    details = {item["id"]: item["detail"] for item in report["results"]}
    assert "-57.40%" in details["store_cliff"] and "1,740,234.97" in details["store_cliff"]
    assert "未观测到" in details["sku_delist"]
    assert "-67.87%" in details["promo_noise"]
    assert "+81.57%" in details["hidden_discount_rate_up"]     # §5.3 新埋点也被独立回验
    assert "无值" in details["store_mix_shift_new_store"]
    assert json.dumps(report, ensure_ascii=False)


def test_verify_tmp_metric_change_pass_and_fail(tmp_dataset):
    """同一份数据:阈值放宽 -> 通过;方向要求反过来 / 区间收窄 -> 不通过。"""
    _write_expectations(tmp_dataset, _HEAD
                        + _change("pass_case", "S1 6 月 -73.33%,落在 [0.6, 0.9]",
                                  direction="down", min_rate=0.60, max_rate=0.90)
                        + _change("fail_direction", "声明为 up,实测是 down", direction="up")
                        + _change("fail_band", "实测 -73.33% 落在 [0.1, 0.2] 之外",
                                  min_rate=0.10, max_rate=0.20))
    report = verify(str(tmp_dataset))
    assert report["dataset_version"] == "9.9.9"
    results = {item["id"]: item for item in report["results"]}
    assert (report["total"], report["passed"]) == (3, 1)
    assert results["pass_case"]["passed"] is True
    assert "-73.33%" in results["pass_case"]["detail"]
    assert results["fail_direction"]["passed"] is False
    assert "要求 up 但变化率非正" in results["fail_direction"]["detail"]
    assert results["fail_band"]["passed"] is False
    assert "|变化率| > max_rate 20.00%" in results["fail_band"]["detail"]


def test_verify_tmp_slice_absent_hit_and_miss(tmp_dataset):
    """P2 在 6 月已消失(通过);P1 仍在(不通过)—— 两条路都要跑到。"""
    _write_expectations(tmp_dataset, _HEAD
                        + _absent("absent_ok", "P2 在 6 月无销量", "P2")
                        + _absent("still_there", "P1 在 6 月仍有销量", "P1"))
    results = {item["id"]: item for item in verify(str(tmp_dataset))["results"]}
    assert results["absent_ok"]["passed"] is True
    assert "未观测到" in results["absent_ok"]["detail"]
    assert results["still_there"]["passed"] is False
    assert "仍可观测" in results["still_there"]["detail"]
    assert "120.00" in results["still_there"]["detail"]


def test_verify_engine_error_is_reported_not_raised(tmp_dataset):
    """指标名写错 -> 引擎抛错,回验必须记「不通过 + 原因」而不是把脚本打崩。"""
    _write_expectations(tmp_dataset, _HEAD + _change("unknown_metric", "语义层里没有这个指标",
                                                     metric="不存在的指标"))
    report = verify(str(tmp_dataset))
    assert report["passed"] == 0
    assert "回验执行失败" in report["results"][0]["detail"]


def test_verify_missing_expectations_raises(tmp_dataset):
    """数据集没有黄金断言 -> 回验无法进行,必须抛错而不是产出 0/0 的「全绿」。"""
    with pytest.raises(MissingExpectationsError, match="不许产出 0/0 全绿"):
        verify(str(tmp_dataset))
    # 显式声明「没有期望」仍然允许:那是作者的选择,报告里也会写明 0/0 不代表通过
    _write_expectations(tmp_dataset, 'dataset_version: "9.9.9"\nexpectations: []\n')
    report = verify(str(tmp_dataset))
    assert (report["total"], report["passed"], report["results"]) == (0, 0, [])


def test_verify_prefers_semantic_version_and_warns(tmp_dataset, capsys):
    """报告绑定的版本以语义层为准(数据实际长什么样),声明不一致要在 stderr 提醒。"""
    _write_expectations(tmp_dataset, 'dataset_version: "1.0.0"\nexpectations: []\n')
    report = verify(str(tmp_dataset))
    assert report["dataset_version"] == "9.9.9"
    assert "不一致" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main:退出码 0 / 1 / 2
# ---------------------------------------------------------------------------
def test_main_exit_codes(tmp_dataset, capsys):
    assert main(["--dataset", "ecommerce-demo"]) == 0                 # 全部通过
    _write_expectations(tmp_dataset, 'dataset_version: "9.9.9"\nexpectations: []\n')
    assert main(["--dataset", str(tmp_dataset)]) == 0                 # 显式空列表 -> 0/0
    _write_expectations(tmp_dataset, _HEAD + _change("fail_band", "-73.33% 落在 [0.1, 0.2] 外",
                                                     min_rate=0.10, max_rate=0.20))
    assert main(["--dataset", str(tmp_dataset)]) == 1                  # 有未通过
    assert main(["--dataset", str(tmp_dataset / "不存在")]) == 2        # 数据集不存在
    assert "FAIL" in capsys.readouterr().out


def test_main_missing_expectations_exits_1_not_0(tmp_dataset, capsys):
    """F7:缺 expectations.yaml 是「无法回验」(1),不是「全过」(0)也不是用法错误(2)。"""
    assert main(["--dataset", str(tmp_dataset)]) == 1
    captured = capsys.readouterr()
    assert "无法回验" in captured.err and "0/0" in captured.err
    assert "PASS" not in captured.out


def test_main_requires_dataset():
    assert main([]) == 2              # 缺 --dataset:用法错误
    assert main(["--no-such-flag"]) == 2
