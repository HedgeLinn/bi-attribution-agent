"""scripts/evaluate_agent.py 的测试(下):mock 剧本、报告与分母自证、过滤、CLI。

接 test_evaluate_agent.py(那一半管 case 解析与四维判定)。三组原则:
① mock 剧本的覆盖面本身就是验收对象——四维每一维都要有失败样本与通过样本,把任一维
   判定改成恒 True,报告必须变化(否则那一维在管线里等于没被验证);
② 分母必须自证:空 case 集 / 过滤后为空 / limit 非正都抛错,报告要写清准确率是谁的;
③ 评分上下文必须用数据集包的**绝对根目录**,且不许泄漏给调用方。
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from harness.datasets import DatasetError
from scripts import eval_report
from scripts.eval_common import depth_levels
from scripts.eval_mock_scripts import SCRIPTS_BY_ID, pick_script
from scripts.evaluate_agent import (evaluate, load_cases, main, mock_run,
                                    parse_conclusion, score_case)
# _cwd_repo / in_repo 是夹具:必须出现在本模块命名空间里才会生效,故忽略「未使用」
from tests.eval_fixtures import (  # noqa: F401
    DATASET, DIMS, FAKE_SEMANTIC, VALID_CASE, _cwd_repo, failed_dims, in_repo,
    make_case, write_case,
)


# --- mock 剧本 --------------------------------------------------------------
def test_mock_run_real_cases_have_discrimination(in_repo):
    """四个出厂 case 的剧本落在不同的判定上:全对 / 三维同错 / 撞禁用短语 / 诚实通过。

    依赖 in_repo 夹具钉住 BI_DATASET:仓库里有多个数据集时,深度维的地图解析
    (四级优先级里的「唯一数据集自动选中」)不再唯一,不钉会抛 DatasetError。
    """
    verdicts = {}
    for case in load_cases(str(DATASET)):
        content, usage = mock_run(case)
        dims = score_case(case, parse_conclusion(content))["dimensions"]
        verdicts[case.id] = (failed_dims(dims), usage)
    assert verdicts["c1_store_cliff"][0] == set()
    assert verdicts["c2_sku_delist"][0] == {"定位命中", "深度达标", "数值准确"}
    assert verdicts["c3_promo_noise"][0] == {"定位命中", "无错误断言"}
    assert verdicts["c4_seasonal"][0] == set()
    assert all(usage["rounds"] > 0 and usage["input_tokens"] > 0 for _, usage in verdicts.values())


def test_mock_factory_cases_pinned_and_rotation_covers_all_archetypes(in_repo):
    """出厂 case 的剧本必须显式绑定(哈希轮换只保证均匀,不保证每一维都有失败样本);
    合成 id 仍按 crc32 轮换,任何 case 集都要能走到六条判定路径(「撞禁用短语」这一路
    需要 case 声明 must_not_claim,没声明时它退化成「全对」,这是剧本的既有语义)。"""
    assert set(SCRIPTS_BY_ID) == {"c1_store_cliff", "c2_sku_delist",
                                  "c3_promo_noise", "c4_seasonal"}
    assert all(pick_script(cid) == script for cid, script in SCRIPTS_BY_ID.items())
    signatures = set()
    for index in range(24):
        case = make_case(id=f"syn-{index}", must_not_claim=("降价促销",))
        content, _ = mock_run(case)
        signature = frozenset(failed_dims(score_case(case, parse_conclusion(content))["dimensions"]))
        signatures.add(signature)
    for expected in (frozenset(), frozenset({"定位命中"}), frozenset({"深度达标"}),
                     frozenset({"数值准确"}), frozenset({"无错误断言"}), frozenset(DIMS)):
        assert expected in signatures, f"缺 {sorted(expected) or '全对'} 剧本"


def test_mock_report_dim_coverage_and_mutation(monkeypatch):
    """F3 的自证:四维每维都要有失败样本与通过样本;把任一维判定改成恒 True,报告必须变。

    恒 True 后报告不变 = 那一维在 mock 集上没有失败样本 = 它在管线里没被真正验证。
    """
    report = evaluate(str(DATASET), mock=True)
    for dim in DIMS:
        assert {r["dimensions"][dim] for r in report["cases"]} == {True, False}, dim
    before = json.dumps(report, ensure_ascii=False)
    for judge in ("locate_ok", "depth_ok", "value_ok", "claim_ok"):
        with monkeypatch.context() as patch:
            patch.setattr(eval_report, judge, lambda *args, **kwargs: True)
            assert json.dumps(evaluate(str(DATASET), mock=True), ensure_ascii=False) != before, judge


# --- 报告 / 分母 / 过滤 ------------------------------------------------------
def test_evaluate_mock_report_numbers_and_denominator():
    """报告结构 + 分母自证:准确率是「过滤 + 截断之后」那个集合的准确率。"""
    report = evaluate(str(DATASET), mock=True)
    assert report["mode"] == "mock" and report["dataset"] == "ecommerce-demo"
    assert report["dataset_version"] == "1.0.0"
    assert (report["total"], report["passed"], report["accuracy"]) == (9, 3, 0.3333)
    assert report["by_category"] == {"零售": {"total": 9, "passed": 3, "accuracy": 0.3333}}
    assert report["by_tier"]["L1"]["accuracy"] == 1.0
    assert report["by_tier"]["L3"] == {"total": 1, "passed": 0, "accuracy": 0.0}
    assert report["limit"] is None                     # 未截断:分母 = 全部 case
    assert report["filters"] == {"case": [], "category": [], "tier": []}
    record = report["cases"][0]
    assert set(record) == {"id", "tier", "category", "passed", "dimensions",
                           "missing_fields", "usage", "conclusion"}
    assert record["missing_fields"] == [] and record["usage"]["rounds"] > 0
    assert json.dumps(report, ensure_ascii=False)      # 报告必须可直接序列化
    for bad in (0, -3):
        with pytest.raises(ValueError, match="limit"):
            evaluate(str(DATASET), mock=True, limit=bad)
    truncated = evaluate(str(DATASET), mock=True, limit=3)
    assert truncated["limit"] == 3 and truncated["total"] == 3
    assert truncated["accuracy"] == round(1 / 3, 4)    # 前三个里只有 c1 全过
    assert truncated["by_category"] == {"零售": {"total": 3, "passed": 1, "accuracy": 0.3333}}
    both = evaluate(str(DATASET), mock=True, tiers=["L2"], limit=1)
    assert (both["total"], both["limit"]) == (1, 1)    # 先过滤再截断
    assert both["filters"] == {"case": [], "category": [], "tier": ["L2"]}


def test_evaluate_sample_fixed_seed_and_denominator():
    """--sample 固定种子:同命令同结果;分母自证 filters.sample;pipeline 顺序 过滤→抽样→截断。"""
    first = evaluate(str(DATASET), mock=True, sample=3)
    second = evaluate(str(DATASET), mock=True, sample=3)
    assert [c["id"] for c in first["cases"]] == [c["id"] for c in second["cases"]]  # 同种子同子集
    assert first["total"] == 3 and first["filters"]["sample"] == ["3"]
    full = evaluate(str(DATASET), mock=True, sample=99)          # 抽满 = 全量,顺序不变
    assert [c["id"] for c in full["cases"]] == [c["id"] for c in evaluate(str(DATASET), mock=True)["cases"]]
    filtered = evaluate(str(DATASET), mock=True, tiers=["L2"], sample=3, limit=2)
    assert filtered["total"] == 2 and filtered["filters"] == {
        "case": [], "category": [], "tier": ["L2"], "sample": ["3"]}
    assert all(c["tier"] == "L2" for c in filtered["cases"])      # 先过滤再抽样
    with pytest.raises(ValueError, match="sample"):
        evaluate(str(DATASET), mock=True, sample=0)
    with pytest.raises(ValueError, match="sample"):
        evaluate(str(DATASET), mock=True, sample=-1)


def test_evaluate_filters_by_case_category_tier():
    """--case 多次取并集(结果按文件名序);三类条件之间是「与」;筛空 / 敲错 id 都抛错。"""
    by_id = evaluate(str(DATASET), mock=True, case_ids=["c4_seasonal", "c1_store_cliff"])
    assert [c["id"] for c in by_id["cases"]] == ["c1_store_cliff", "c4_seasonal"]
    assert by_id["filters"]["case"] == ["c1_store_cliff", "c4_seasonal"]
    assert (by_id["total"], by_id["passed"]) == (2, 2)
    both = evaluate(str(DATASET), mock=True, tiers=["L2"], categories=["零售"])
    assert [c["id"] for c in both["cases"]] == [
        "c1_store_cliff", "c3_promo_noise", "e1_pure_volume", "e2_pure_price",
        "e4_volume_price_reversal", "e5_store_mix_shift", "e6_hidden_discount"]
    assert both["filters"] == {"case": [], "category": ["零售"], "tier": ["L2"]}
    with pytest.raises(ValueError, match="过滤后没有 case"):        # 交集为空
        evaluate(str(DATASET), mock=True, case_ids=["c1_store_cliff"], categories=["不存在的分类"])
    with pytest.raises(ValueError, match="过滤后没有 case"):
        evaluate(str(DATASET), mock=True, tiers=["L9"])
    with pytest.raises(ValueError, match="指定的 case 不存在.*nope"):   # 敲错 id 不许出报告
        evaluate(str(DATASET), mock=True, case_ids=["c1_store_cliff", "nope"])


def test_evaluate_mock_is_offline_and_outside_repo(tmp_path, monkeypatch):
    """mock 不碰数据、不需要 Key;数据集包在仓库外时深度维按该包自己的地图判。

    后者是 F1 的回归:旧实现把 BI_DATASET 写成数据集的 **id**,深度维再按 CWD 的 datasets/
    解析——包在仓库外时地图丢失、退化成「同层才算达标」,把更深的正确答案判成不达标
    (假失败、无日志)。这里把 c1 的 required_depth 改浅一层,剧本给的门店层答案就比要求
    更深:地图丢了必失败,地图在与仓库内报告逐值一致。
    """
    monkeypatch.delenv("BI_API_KEY", raising=False)
    root = tmp_path / "fake-demo"
    (root / "cases").mkdir(parents=True)
    (root / "dataset.yaml").write_text('id: fake-demo\ntitle: 假数据集\nversion: "0.0.1"\n',
                                       encoding="utf-8")
    (root / "semantic.yaml").write_text(FAKE_SEMANTIC, encoding="utf-8")
    write_case(root / "cases", "a.yaml", VALID_CASE)
    report = evaluate(str(root), mock=True)
    assert report["total"] == 1 and report["dataset"] == "fake-demo"
    assert report["dataset_version"] == "0.0.1"
    assert evaluate(str(DATASET), mock=True, limit=2)["total"] == 2
    repo_report = evaluate(str(DATASET), mock=True)
    copy = tmp_path / "ecommerce-demo"
    shutil.copytree(DATASET, copy)
    case_path = copy / "cases" / "c1_store_cliff.yaml"
    case_path.write_text(case_path.read_text(encoding="utf-8").replace(
        "required_depth: store_id", "required_depth: region"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BI_DATASET", raising=False)
    outside = evaluate(str(copy), mock=True)
    assert (outside["total"], outside["passed"]) == (9, 3)
    assert outside == repo_report                  # 含 dataset / version / 逐 case 的维度明细
    assert "BI_DATASET" not in os.environ          # 评分上下文不许泄漏给调用方


def test_depth_levels_raises_when_map_unavailable(tmp_path, monkeypatch):
    """地图解析不到必须抛错:静默退化成「同层达标」= 更深的正确答案被判假失败。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BI_DATASET", str(tmp_path / "不存在的数据集"))
    with pytest.raises(DatasetError, match="深度维"):
        depth_levels()


def test_evaluate_live_requires_api_key(monkeypatch):
    """真跑分支写完但本机无 Key:缺 Key 要当场报错,而不是跑一半才炸。

    两种协议的凭证都要删:本机环境变量可能持久化了 ANTHROPIC_AUTH_TOKEN
    (作者机器如此),只删 BI_API_KEY 会被 Anthropic 分支放行,跑到 live
    内部才炸出别的错误。"""
    monkeypatch.delenv("BI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="BI_API_KEY"):
        evaluate(str(DATASET), mock=False, limit=1)


# --- CLI 与数据集约束 --------------------------------------------------------
def test_main_usage_errors(tmp_path, capsys):
    assert main([]) == 2                                          # 缺 --dataset
    assert main(["--dataset", str(tmp_path / "nope")]) == 2       # 数据集不存在
    assert main(["--dataset", "ecommerce-demo", "--limit", "-1"]) == 2
    assert main(["--dataset", "ecommerce-demo", "--limit", "0"]) == 2
    assert main(["--dataset", "ecommerce-demo", "--limit", "两个"]) == 2
    assert main(["--dataset", "ecommerce-demo", "--case", "没有这个"]) == 2
    assert "正整数" in capsys.readouterr().err


def test_main_mock_writes_report_and_prints_denominator(tmp_path, capsys):
    out = tmp_path / "report.json"
    assert main(["--dataset", "ecommerce-demo", "--mock", "--tier", "L2",
                 "--case", "c3_promo_noise", "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["total"] == 1 and report["mode"] == "mock"
    assert [c["id"] for c in report["cases"]] == ["c3_promo_noise"]
    assert "过滤:" in capsys.readouterr().out
    assert main(["--dataset", "ecommerce-demo", "--mock", "--limit", "2",
                 "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert (report["total"], report["limit"]) == (2, 2)
    assert [c["id"] for c in report["cases"]] == ["c1_store_cliff", "c2_sku_delist"]
    printed = capsys.readouterr().out
    assert "已按 --limit 2 截断" in printed and "过滤:" not in printed


def test_slice_occupancy_is_isolated_or_declared():
    """任意两 case 的影响集合不得相交;相交的必须双向声明为「允许叠加」。"""
    occupancy = {c.id: (c.raw["slice_occupancy"], set(c.raw.get("overlap_with") or ()))
                 for c in load_cases(str(DATASET))}
    for left, (left_occ, left_declared) in occupancy.items():
        for right, (right_occ, right_declared) in occupancy.items():
            if left >= right:
                continue
            same_window = (left_occ["window"][0] <= right_occ["window"][1]
                           and right_occ["window"][0] <= left_occ["window"][1])
            # 全局量(keys 为空)覆盖所有切片;否则按切片键判交
            same_slice = (not left_occ["keys"] or not right_occ["keys"]
                          or set(left_occ["keys"]) & set(right_occ["keys"]))
            if same_window and same_slice:
                assert right in left_declared and left in right_declared, \
                    f"{left} 与 {right} 影响集合相交但未双向声明"
