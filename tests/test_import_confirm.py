"""人工确认向导(harness/import_confirm.py 等)的验收。

本文件的核心是**一条回归测试**:自动推断出的地图合法但可以算错,而错的量级是
几十倍 —— 这正是整套确认流程存在的理由(docs/REUSE_DESIGN.md §6.2 B 档)。
其余用例覆盖拆口径、落位契约、闸门、清理与各项输入校验。

导入产生的 id 带时间戳,测试一律用 tmp_path 隔离,绝不写 datasets/。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml

from attribution.engine import AttributionEngine
from attribution.semantic import Semantic
from attribution.sql_source import introspect_columns
from harness.datasets import discover_datasets
from harness.import_answers import apply_answers
from harness.import_confirm import (
    CONFIRMED_VERSION,
    ConfirmError,
    cleanup_stale,
    commit,
    count_uncovered,
    discard,
    list_level_values,
    list_pending,
    list_unconfirmed,
    load_pending,
    preview,
    skip,
    table_rows,
    verify,
)
from harness.import_metric import split_metric
from harness.importer import DRAFT_NAME, dump_yaml, import_upload, pending_dir

# 月度锯齿:balance 是「月内累计、跨月归零」的余额型列,amount 是恒定的日流量。
# 两个指标都会被自动推断判成 semi_additive + last,但两个都不该是:
#   - balance:月内递增,相邻日对子 81/83 ≈ 97.6% 满足「后 ≥ 前」→ 命中 90% 阈值
#   - amount :恒定值天然满足「后 ≥ 前」(100%)→ **常量被读成「单调不减」**
# 逐日真值(每天两行渠道,故逐日 SUM = 2 × 序号):
#   May 2×(1+…+31)=992,Jun 2×(1+…+30)=930,Jul 2×(1+…+23)=552 → 合计 2474
_TRUE_BALANCE_TOTAL = 2474     # 逐日求和:balance 在整个窗口上的真值
_LAST_DAY_BALANCE = 46         # 窗口最后一天(07-23)的值 = semi_additive + last 会取到的数
_TRUE_AMOUNT_TOTAL = 168       # 2 × 84 天
_LAST_DAY_AMOUNT = 2
_WINDOW = ("2026-05-01", "2026-07-23")

# 向导第 ② 步的答案:把两个指标都改回「加法 + 逐期求和」
_FIX_ANSWERS = {"metrics": [{"name": "balance_sum", "type": "additive"},
                            {"name": "amount_sum", "type": "additive"}]}

# 渠道小表:日总额 150 / 180 / 120,不单调(150→180 升、180→120 降 = 50%)
_CHANNEL_CSV = """date,channel,amount
2026-06-01,online,100
2026-06-01,offline,50
2026-06-02,online,120
2026-06-02,offline,60
2026-06-03,online,80
2026-06-03,offline,40
"""


def _sawtooth_csv() -> bytes:
    """生成月度锯齿 CSV(84 天 × 2 渠道 = 168 行)。"""
    lines = ["date,channel,amount,balance"]
    for year, month, days in ((2026, 5, 31), (2026, 6, 30), (2026, 7, 23)):
        for day in range(1, days + 1):
            for channel in ("online", "offline"):
                lines.append(f"{year:04d}-{month:02d}-{day:02d},{channel},1,{day}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _import(tmp_path: Path, name: str, data: bytes) -> dict:
    result = import_upload(name, data, datasets_dir=tmp_path)
    assert result["ok"] is True, result
    return result


def _engine(tmp_path: Path, dataset_id: str, semantic_name: str = "semantic.yaml"):
    package = tmp_path / dataset_id
    return AttributionEngine(str(package / "data"), str(package / semantic_name))


def _total(engine, metric: str) -> float:
    return engine.query_metric(metric, [], {}, *_WINDOW)["total"]


# ---------------------------------------------------------------------------
# 核心回归:合法却算错的地图,必须被人工确认流程拦住
# ---------------------------------------------------------------------------
def test_auto_draft_misjudges_sawtooth_as_semi_additive(tmp_path) -> None:
    """**先把缺陷钉死**:自动推断把两个指标都判成 semi_additive,整窗差 54 倍。

    这条不是「测一个已知的正确行为」,而是记录「不做人工确认会发生什么」:
    balance 取期末值 46,真值 2474;amount 取期末值 2,真值 168。
    """
    result = _import(tmp_path, "sawtooth.csv", _sawtooth_csv())
    draft = load_pending(result["id"], tmp_path).draft
    for name in ("balance_sum", "amount_sum"):
        assert draft["metrics"][name]["type"] == "semi_additive"
        assert draft["metrics"][name]["time_aggregation"] == "last"

    # 跳过确认 == 按自动地图落位,于是这份 54 倍偏差真的进入了可分析数据集
    skip(result["id"], datasets_dir=tmp_path)
    engine = _engine(tmp_path, result["id"])
    assert _total(engine, "balance_sum") == _LAST_DAY_BALANCE      # 46,不是 2474
    assert _total(engine, "amount_sum") == _LAST_DAY_AMOUNT        # 2,不是 168


def test_confirmed_answers_fix_the_sawtooth_caliber(tmp_path) -> None:
    """确认向导把口径改回加法后,整窗值回到真值 —— 这是本流程存在的全部意义。"""
    result = _import(tmp_path, "sawtooth.csv", _sawtooth_csv())
    commit(result["id"], _FIX_ANSWERS, datasets_dir=tmp_path)
    engine = _engine(tmp_path, result["id"])
    assert _total(engine, "balance_sum") == _TRUE_BALANCE_TOTAL    # 2474,不再是 46
    assert _total(engine, "amount_sum") == _TRUE_AMOUNT_TOTAL      # 168,不再是 2

    semantic = yaml.safe_load((tmp_path / result["id"] / "semantic.yaml")
                              .read_text(encoding="utf-8"))
    assert semantic["metrics"]["balance_sum"]["type"] == "additive"
    assert "time_aggregation" not in semantic["metrics"]["balance_sum"]


def test_half_additive_answer_still_gates_on_smoke(tmp_path) -> None:
    """半可加 + last 是合法口径(余额型列就该这么写),闸门不该把它当成错误。"""
    result = _import(tmp_path, "sawtooth.csv", _sawtooth_csv())
    commit(result["id"], {"metrics": [{"name": "balance_sum", "type": "semi_additive"}]},
           datasets_dir=tmp_path)
    assert _total(_engine(tmp_path, result["id"]), "balance_sum") == _LAST_DAY_BALANCE


# ---------------------------------------------------------------------------
# 拆口径:一个列 + 一个维度取值 = 一个业务指标
# ---------------------------------------------------------------------------
def test_split_metric_builds_conditional_sum(tmp_path) -> None:
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    entry = load_pending(result["id"], tmp_path).draft["metrics"]["amount_sum"]

    name, one = split_metric(entry, "amount_sum", "channel", ["online"], "amount_sum_online")
    assert name == "amount_sum_online"
    assert one["expression"] == "SUM(CASE WHEN channel = 'online' THEN amount ELSE 0 END)"
    assert one["type"] == "additive"
    assert one["depends_on"] == ["channel", "amount"]      # 拆分列 + 目标列,现场推导
    assert list(one)[:2] == ["label", "expression"]        # 键序 = 既有语义层的约定

    _, many = split_metric(entry, "amount_sum", "channel", ["online", "offline"], "both")
    assert many["expression"] == (
        "SUM(CASE WHEN channel IN ('online', 'offline') THEN amount ELSE 0 END)")


def test_split_metric_rejects_expression_it_cannot_split(tmp_path) -> None:
    """比率 / 多聚合的表达式拆不出来:显式报错,而不是猜一个看起来对的结果。"""
    with pytest.raises(ConfirmError, match="拆不了"):
        split_metric({"expression": "SUM(a) / NULLIF(SUM(b), 0)"},
                     "aov", "channel", ["online"], "aov_online")
    with pytest.raises(ConfirmError, match="至少要勾选一个取值"):
        split_metric({"expression": "SUM(a)"}, "a", "channel", [], "a_x")


def test_committed_split_metric_is_queryable(tmp_path) -> None:
    """拆出的指标要真的能过闸门并被引擎查出来(口径生成的端到端)。"""
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    commit(result["id"], {"metrics": [{"name": "amount_sum", "splits": [
        {"column": "channel", "values": ["online"]}]}]}, datasets_dir=tmp_path)
    engine = _engine(tmp_path, result["id"])
    assert _total(engine, "amount_sum_online") == 300        # 100 + 120 + 80


def test_count_uncovered_reports_the_gap(tmp_path) -> None:
    """Σ子项 ≠ 父指标的缺口要被量出来(SQL 里 NULL 不等于任何值),而不是猜。"""
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    data_dir = pending_dir(tmp_path) / result["id"] / "data"
    assert count_uncovered(data_dir, "fact", "channel", ["online"]) == 3
    assert count_uncovered(data_dir, "fact", "channel", ["online", "offline"]) == 0
    assert count_uncovered(data_dir, "fact", "channel", []) is None       # 问不出来 ≠ 0
    assert count_uncovered(data_dir, "fact", "nope", ["online"]) is None


def test_list_level_values_is_sorted_and_deduped(tmp_path) -> None:
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    data_dir = pending_dir(tmp_path) / result["id"] / "data"
    assert list_level_values(data_dir, "fact", "channel") == ["offline", "online"]
    assert list_level_values(data_dir, "fact", "channel", limit=1) == ["offline"]
    assert list_level_values(data_dir, "fact", "nope") == []      # 列不存在:给空,不崩


def test_derived_metric_requires_both_sides(tmp_path) -> None:
    """新增派生比率:分子分母都必填、不能同一个(分母自带 NULLIF 除零守卫)。"""
    draft = {"metrics": {"amount_sum": {"expression": "SUM(amount)", "type": "additive",
                                        "depends_on": ["amount"]}}}
    out = apply_answers(draft, {"metrics": [{"name": "aov", "type": "derived", "derived": {
        "numerator": "amount_sum", "denominator": "orders"}}]})
    assert out["metrics"]["aov"]["expression"] == "amount_sum / NULLIF(orders, 0)"
    assert out["metrics"]["aov"]["depends_on"] == ["amount_sum", "orders"]
    with pytest.raises(ConfirmError, match="分子与分母"):
        apply_answers(draft, {"metrics": [{"name": "aov", "derived": {"numerator": "x"}}]})


# ---------------------------------------------------------------------------
# 输入校验:答案里的错要在这里报,不能写进地图
# ---------------------------------------------------------------------------
def test_duplicate_metric_name_is_rejected(tmp_path) -> None:
    """重名会静默覆盖,而覆盖掉的往往正是用户刚调好的那条。"""
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    with pytest.raises(ConfirmError, match="重名"):
        commit(result["id"], {"metrics": [{"name": "amount_sum", "splits": [
            {"column": "channel", "values": ["online"], "name": "amount_sum"}]}]},
            datasets_dir=tmp_path)


def test_calendar_range_must_be_iso_date(tmp_path) -> None:
    """写错格式的日期会被消费方静默跳过,所以必须在这里报错。"""
    draft = {"metrics": {}}
    ok = apply_answers(draft, {"calendar": {"promo": [
        {"name": "618", "range": ["2026-06-01", "2026-06-18"], "note": "脉冲"}]}})
    assert ok["time"]["calendar"]["promo"][0]["range"] == ["2026-06-01", "2026-06-18"]
    with pytest.raises(ConfirmError, match="YYYY-MM-DD"):
        apply_answers(draft, {"calendar": {"promo": [
            {"name": "618", "range": ["6/1", "2026-06-18"]}]}})


def test_caveats_string_becomes_list(tmp_path) -> None:
    """caveats 必须是 list[str]:一整段文本会被 context._read_caveats 静默忽略。"""
    out = apply_answers({"metrics": {}}, {"caveats": "余额是时点值\n\n不能跨月求和\n"})
    assert out["caveats"] == ["余额是时点值", "不能跨月求和"]


def test_empty_calendar_is_dropped(tmp_path) -> None:
    """日历名为空 / 起止不全的条目丢弃;一条都不剩时不留下空 time 段。"""
    assert "time" not in apply_answers({"metrics": {}}, {"calendar": {"": [
        {"name": "618", "range": ["2026-06-01", "2026-06-18"]}]}})
    assert "time" not in apply_answers({"metrics": {}}, {"calendar": {"promo": [
        {"name": "618", "range": ["2026-06-01"]}]}})


# ---------------------------------------------------------------------------
# 闸门:写盘前自证 + 冒烟
# ---------------------------------------------------------------------------
def test_verify_reports_problems_without_raising(tmp_path) -> None:
    """verify 只收集问题、不抛异常;而**自动草稿本身是合法的** —— 危险正在这里。"""
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    data_dir = pending_dir(tmp_path) / result["id"] / "data"
    draft = load_pending(result["id"], tmp_path).draft
    assert verify(draft, data_dir) == []

    problems = verify({**draft, "fact_table": ""}, data_dir)
    assert problems and "fact_table" in problems[0]
    assert introspect_columns(str(data_dir))             # 探针:列真的从 parquet 读到了


def test_commit_rejects_inconsistent_depends_on_and_writes_nothing(tmp_path) -> None:
    """草稿被改坏(depends_on 与表达式引用不一致)→ 拦住,且磁盘上不留半份包。

    直接改盘上的草稿,是为了证明闸门**真的在拦**:空答案不会重算 depends_on,
    所以被改坏的那份会原样走到 verify 面前。
    """
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    draft_path = pending_dir(tmp_path) / result["id"] / DRAFT_NAME
    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    draft["metrics"]["amount_sum"]["depends_on"] = ["not_a_column"]
    dump_yaml(draft_path, draft)

    with pytest.raises(ConfirmError, match="尚未落位"):
        commit(result["id"], {}, datasets_dir=tmp_path)
    assert not (tmp_path / result["id"]).exists()                 # 不落位
    assert (pending_dir(tmp_path) / result["id"]).is_dir()        # 待确认包保留,可重试


def test_commit_on_missing_package_raises(tmp_path) -> None:
    with pytest.raises(ConfirmError, match="不存在或已被清理"):
        commit("upload_nope_20260101000000", {}, datasets_dir=tmp_path)


# ---------------------------------------------------------------------------
# 落位契约:确认 / 跳过 / 放弃 / 隔离
# ---------------------------------------------------------------------------
def test_commit_places_dataset_and_clears_pending(tmp_path) -> None:
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    out = commit(result["id"], {}, datasets_dir=tmp_path)
    assert out["ok"] is True and out["id"] == result["id"]
    assert out["metrics"] == ["amount_sum"] and out["dimensions"] == ["channel"]

    package = tmp_path / result["id"]
    assert (package / "dataset.yaml").is_file()
    assert (package / "semantic.yaml").is_file()
    assert (package / "data" / "fact.parquet").is_file()
    assert not (pending_dir(tmp_path) / result["id"]).exists()
    assert not (package / "semantic.commit.yaml").exists()   # 冒烟用的临时文件随包被删

    manifest = yaml.safe_load((package / "dataset.yaml").read_text(encoding="utf-8"))
    assert "unconfirmed" not in manifest
    semantic = yaml.safe_load((package / "semantic.yaml").read_text(encoding="utf-8"))
    assert semantic["dataset_version"] == CONFIRMED_VERSION


def test_skip_marks_dataset_unconfirmed(tmp_path) -> None:
    """跳过 ≠ 绕过闸门:仍然能落位、能被分析,但标记为未确认,以后可回来补确认。"""
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    assert skip(result["id"], datasets_dir=tmp_path)["unconfirmed"] is True

    manifest = yaml.safe_load((tmp_path / result["id"] / "dataset.yaml")
                              .read_text(encoding="utf-8"))
    assert manifest["unconfirmed"] is True
    assert semantic_version(tmp_path, result["id"]) == "import"   # 没过人工闸门的版本号

    ids = [info.id for info in list_unconfirmed(tmp_path)]
    assert ids == [result["id"]]
    assert discover_datasets(tmp_path)[0].unconfirmed is True


def semantic_version(tmp_path: Path, dataset_id: str) -> str:
    raw = yaml.safe_load((tmp_path / dataset_id / "semantic.yaml").read_text(encoding="utf-8"))
    return raw["dataset_version"]


def test_pending_package_is_invisible_to_discovery(tmp_path) -> None:
    """隔离的核心:没落位的包不能被发现、不能被「唯一数据集自动选中」选中。"""
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    assert list_pending(tmp_path) == [result["id"]]
    assert discover_datasets(tmp_path) == []

    discard(result["id"], datasets_dir=tmp_path)
    assert list_pending(tmp_path) == []
    assert discover_datasets(tmp_path) == []


def test_skip_then_commit_is_not_possible(tmp_path) -> None:
    """落位后待确认包就没了 —— 未确认的数据集要补确认,得重新导入(本轮不做改图入口)。"""
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    skip(result["id"], datasets_dir=tmp_path)
    with pytest.raises(ConfirmError, match="不存在或已被清理"):
        commit(result["id"], {}, datasets_dir=tmp_path)


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------
def test_cleanup_stale_removes_old_keeps_fresh(tmp_path) -> None:
    old = _import(tmp_path, "old.csv", _CHANNEL_CSV.encode("utf-8"))["id"]
    fresh = _import(tmp_path, "fresh.csv", _CHANNEL_CSV.encode("utf-8"))["id"]
    stale_time = time.time() - 48 * 3600
    os.utime(pending_dir(tmp_path) / old, (stale_time, stale_time))

    assert cleanup_stale(tmp_path, max_age_hours=24) == [old]
    assert list_pending(tmp_path) == [fresh]


def test_cleanup_stale_never_removes_the_session_package(tmp_path) -> None:
    """用户把向导晾了一天,按 mtime 删掉他正在改的那份是这套策略唯一会伤人的情形。"""
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    stale_time = time.time() - 48 * 3600
    os.utime(pending_dir(tmp_path) / result["id"], (stale_time, stale_time))

    assert cleanup_stale(tmp_path, max_age_hours=24, keep=[result["id"]]) == []
    assert list_pending(tmp_path) == [result["id"]]


def test_preview_shows_both_calibers_side_by_side(tmp_path) -> None:
    """向导第 ② 步的全部依据:同一份数据,两种口径下的整窗值差 54 倍。

    用户要在「加法」和「半可加」之间做选择 —— 不把这两个数字摆出来,他就是盲选。
    """
    result = _import(tmp_path, "sawtooth.csv", _sawtooth_csv())
    pkg = load_pending(result["id"], tmp_path)

    as_is = preview(apply_answers(pkg.draft, None), pkg.data_dir, pkg.root)
    assert as_is["balance_sum"] == _LAST_DAY_BALANCE            # 自动推断:半可加,46
    fixed = preview(apply_answers(pkg.draft, _FIX_ANSWERS), pkg.data_dir, pkg.root)
    assert fixed["balance_sum"] == _TRUE_BALANCE_TOTAL          # 改成加法:2474


def test_preview_reports_unloadable_map_instead_of_raising(tmp_path) -> None:
    """界面上实时改口径,中途必然出现不合法的地图 —— 要报出来,不能让页面崩掉。"""
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    pkg = load_pending(result["id"], tmp_path)
    assert preview({"metrics": {}}, pkg.data_dir, pkg.root)["error"]


def test_table_rows_counts_the_fact_table(tmp_path) -> None:
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    data_dir = pending_dir(tmp_path) / result["id"] / "data"
    assert table_rows(data_dir, "fact") == 6
    assert table_rows(data_dir, "nope") is None


def test_calendar_reaches_the_confirmed_map(tmp_path) -> None:
    """第 ④ 步的世界知识(口径陷阱 + 促销日历)真的进了地图,不是只留在界面上。"""
    result = _import(tmp_path, "channel.csv", _CHANNEL_CSV.encode("utf-8"))
    commit(result["id"], {
        "caveats": ["渠道口径含退货,不可与财务口径直接比"],
        "calendar": {"promo": [{"name": "618", "range": ["2026-06-01", "2026-06-18"],
                                "note": "大促脉冲是预期"}]},
    }, datasets_dir=tmp_path)
    raw = yaml.safe_load((tmp_path / result["id"] / "semantic.yaml")
                         .read_text(encoding="utf-8"))
    assert raw["caveats"] == ["渠道口径含退货,不可与财务口径直接比"]
    assert raw["time"]["calendar"]["promo"][0]["name"] == "618"

    # 装了引擎还能装得动(日历的日期是真的能被消费方解析的)
    engine = _engine(tmp_path, result["id"])
    assert _total(engine, "amount_sum") is not None