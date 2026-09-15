"""scripts/evaluate_agent.py 的测试(上):case 解析、结论解析、四维评分。

三组原则(为守单文件 300 行的上限做过用例合并,断言一条没删;mock 剧本 / 报告 /
分母 / 过滤 / CLI / 数据集约束在 test_eval_pipeline.py——同一轮拆分):
① 评分器对模型输出的**形状**必须完全容错(再怪也只返回 False,不许抛错),而 case
   文件本身必须严格(坏 case 要炸,否则准确率的分母会悄悄缩小);
② 「case 没要求就不评」与「§5.8 必填字段必须被强制」是一对边界,两侧都要有用例;
③ 评分上下文(BI_DATASET)由夹具钉住:score_case 的签名已冻结,没有数据集参数。
"""

from __future__ import annotations

import pytest

from scripts.evaluate_agent import load_cases, parse_conclusion, score_case
# _cwd_repo / in_repo 是夹具:必须出现在本模块命名空间里才会生效,故忽略「未使用」
from tests.eval_fixtures import (  # noqa: F401
    DATASET, DIMS, VALID_CASE, _cwd_repo, failed_dims, in_repo, make_case,
    make_conclusion, write_case,
)


# --- case 解析 --------------------------------------------------------------
def test_load_cases_real_dataset():
    cases = load_cases(str(DATASET))
    assert [c.id for c in cases] == ["c1_store_cliff", "c2_sku_delist",
                                     "c3_promo_noise", "c4_seasonal",
                                     "e1_pure_volume", "e2_pure_price",
                                     "e4_volume_price_reversal", "e5_store_mix_shift",
                                     "e6_hidden_discount"]
    c1, c2, c3, c4 = cases[:4]
    assert c1.root_cause_slice["key"] == "STORE_S0001"
    assert c1.contribution_range == (0.30, 0.75) and c1.required_depth == "store_id"
    assert c2.root_cause_slice == {"dimension": "product", "level": "product_id",
                                   "key": "SKU_P0001", "label": "数码家电手机通讯-001号"}
    assert c3.root_cause_slice is None and c3.contribution_range is None
    assert c4.root_cause_slice is None and c4.tier == "L1"
    assert c1.raw["slice_occupancy"]["keys"] == ["STORE_S0001"]
    assert all(c.must_not_claim for c in cases)   # 漏读它「无错误断言」会静默变成空断言


def test_load_cases_ok_missing_dir_and_empty_dir(tmp_path):
    write_case(tmp_path / "cases", "a.yaml", VALID_CASE)
    cases = load_cases(str(tmp_path))
    assert len(cases) == 1 and cases[0].contribution_range is None
    assert cases[0].must_not_claim == ()
    with pytest.raises(FileNotFoundError):             # 目录不存在 = 用法错误
        load_cases(str(tmp_path / "nope"))
    (tmp_path / "empty" / "cases").mkdir(parents=True)
    with pytest.raises(ValueError, match="没有任何"):    # 空 case 集 = 0 分母的「全绿」
        load_cases(str(tmp_path / "empty"))


@pytest.mark.parametrize("body", [
    VALID_CASE.replace('  mechanism: "机制"', ""),                 # 缺 expected.mechanism
    VALID_CASE.replace("required_depth: store_id", ""),            # 缺 required_depth
    VALID_CASE.replace('question: "问?"\n', ""),                   # 缺 question
    VALID_CASE.replace("{dimension: store, level: store_id, key: STORE_X}",
                       "{dimension: store}"),                      # 切片缺 key
    VALID_CASE + "  contribution_range: [0.1]\n",                 # 区间不是一个数对
    "id: t1\ntier: L2\n",                                          # 缺 expected
    "- 只是一个列表\n",                                             # 顶层不是映射
    "id: t1\n  bad: indent\n",                                     # 非法 YAML
])
def test_load_cases_bad_case_raises(tmp_path, body):
    """坏 case 必须显式失败(不许静默跳过:准确率的分母会因此缩小)。"""
    write_case(tmp_path / "cases", "bad.yaml", body)
    with pytest.raises(Exception):
        load_cases(str(tmp_path))


def test_parse_conclusion_plain_fenced_and_prose():
    assert parse_conclusion('{"结论": "a"}') == {"结论": "a"}
    assert parse_conclusion('```json\n{"结论": "a"}\n```') == {"结论": "a"}
    assert parse_conclusion('```\n{"结论": "a"}\n```') == {"结论": "a"}
    # 散文前后缀 / 嵌套花括号(取首尾括号之间那段,与 harness.loop 同口径)
    assert parse_conclusion('说明文字前置 {"结论": "a"} 后缀') == {"结论": "a"}
    assert parse_conclusion('```json\n{"结论": "{内层}"}\n```') == {"结论": "{内层}"}


@pytest.mark.parametrize("content", ["", "   ", "没有 JSON", "[1, 2]", '"字符串"', "{坏 JSON"])
def test_parse_conclusion_unparsable(content):
    assert parse_conclusion(content) is None


# --- 四维评分 ---------------------------------------------------------------
def test_score_case_all_correct_and_unparsable(in_repo):
    scored = score_case(make_case(), make_conclusion(), {"rounds": 3})
    assert scored["passed"] is True and not failed_dims(scored["dimensions"])
    assert scored["case_id"] == "t1" and scored["usage"] == {"rounds": 3}
    assert scored["missing_fields"] == []
    assert score_case(make_case(), None)["dimensions"] == dict.fromkeys(DIMS, False)
    assert score_case(make_case(), None)["missing_fields"] == []    # 不可解析 ≠ 缺字段
    # 置信度越界 = 没按 §5.8 的格式回答,与不可解析同判
    weird = score_case(make_case(), make_conclusion(置信度="very-high"))
    assert not any(weird["dimensions"].values())


@pytest.mark.parametrize("over,conclusion_over,failed", [
    ({}, {"根因": {"dimension": "store", "level": "store_id", "key": "STORE_Y"}}, {"定位命中"}),
    ({}, {"根因": {"dimension": "store", "level": "region", "key": "STORE_X"}}, {"深度达标"}),
    ({}, {"量级": {"贡献度": 0.95}}, {"数值准确"}),
    ({}, {"结论": "STORE_X 下滑源于降价促销"}, {"无错误断言"}),         # 撞 must_not_claim
    ({}, {"已排除": ["渠道结构变化", "整体自然回落"]}, {"无错误断言"}),
])
def test_score_case_one_dim_at_a_time(in_repo, over, conclusion_over, failed):
    """一维错只扣一维(定位错 / 层级浅 / 数值出界 / 撞禁用短语)。"""
    case = make_case(must_not_claim=("降价促销", "整体自然回落"), **over)
    scored = score_case(case, make_conclusion(**conclusion_over))
    assert failed_dims(scored["dimensions"]) == failed and scored["passed"] is False
    assert scored["missing_fields"] == []


def test_score_case_boundaries(in_repo):
    """边界都算过:贡献度取闭区间端点;层级比 required_depth 更深也算达标。"""
    assert score_case(make_case(), make_conclusion(量级={"贡献度": 0.30}))["passed"] is True
    assert score_case(make_case(), make_conclusion(量级={"贡献度": 0.70}))["passed"] is True
    assert score_case(make_case(required_depth="region"), make_conclusion())["passed"] is True
    assert score_case(make_case(must_not_claim=("降价促销",)), make_conclusion())["passed"] is True


def test_score_case_value_dim_rules(in_repo):
    """case 没声明区间 -> 数值维跳过=通过(「case 没要求就不评」);声明了却给不出可用
    贡献度 -> 不通过(§5.8 的必填字段必须被强制,否则「不写就不会错」)。"""
    no_range = make_case(contribution_range=None)
    for scale in ({}, None, {"贡献度": None}, {"贡献度": 99}):
        assert score_case(no_range, make_conclusion(量级=scale))["passed"] is True
    for scale in ({}, None, {"贡献度": None}, {"贡献度": "abc"}, {"贡献度": True}):
        scored = score_case(make_case(), make_conclusion(量级=scale))
        assert failed_dims(scored["dimensions"]) == {"数值准确"} and scored["passed"] is False


@pytest.mark.parametrize("dropped,failed", [
    ("结论", {"无错误断言"}), ("已排除", {"无错误断言"}), ("量级", {"数值准确"}),
    ("根因", {"定位命中", "深度达标"}), ("证据链", set()), ("置信度", set()),
])
def test_score_case_missing_required_field(in_repo, dropped, failed):
    """§5.8 的七个必填字段:缺哪个扣哪一维(不牵连其它维),并记进 missing_fields。

    「证据链」没有对应维(只标注);「置信度」缺失不算格式错(scorable 只管越界)。
    """
    conclusion = make_conclusion()
    del conclusion[dropped]
    scored = score_case(make_case(), conclusion)
    assert scored["missing_fields"] == [dropped]
    assert failed_dims(scored["dimensions"]) == failed


def test_score_case_missing_fields_order_and_skip_rules(in_repo):
    """missing_fields 按 §5.8 顺序;两条例外都属于「case 没要求就不评」那一类。"""
    short = {k: v for k, v in make_conclusion().items() if k not in ("量级", "证据链")}
    assert score_case(make_case(), short)["missing_fields"] == ["量级", "证据链"]
    skipped = score_case(make_case(contribution_range=None), short)   # 缺量级不扣分
    assert skipped["passed"] is True
    null_root = make_case(root_cause_slice=None, required_depth="date_id",
                          contribution_range=None)
    honest = make_conclusion(根因=None, 无法归因=True, 量级={"贡献度": None})
    without_root = {k: v for k, v in honest.items() if k != "根因"}
    scored = score_case(null_root, without_root)
    assert scored["missing_fields"] == ["根因"] and scored["passed"] is True   # 缺根因不扣分
    without_flag = {k: v for k, v in honest.items() if k != "无法归因"}
    scored = score_case(null_root, without_flag)
    assert scored["missing_fields"] == ["无法归因"] and scored["passed"] is False
    assert scored["dimensions"]["定位命中"] is False and scored["dimensions"]["深度达标"] is True


def test_score_case_root_case_contradiction_penalized(in_repo):
    """有期望根因的 case 宣布「无法归因」却给(正确)key:定位/深度两维不作数,其余照常判。"""
    sneaky = make_conclusion(无法归因=True, 量级={"贡献度": 0.5})
    dims = score_case(make_case(), sneaky)["dimensions"]
    assert failed_dims(dims) == {"定位命中", "深度达标"}
    assert dims["数值准确"] is True and dims["无错误断言"] is True
    # 对照组:同一 case 不说无法归因 -> 全过(说明扣的是「矛盾」,不是别的)
    assert score_case(make_case(), make_conclusion())["passed"] is True


def test_score_case_unattributable_and_mutual_exclusion(in_repo):
    """null-root case:定位维改判诚实度、深度维跳过;且「无法归因」与「根因」**互斥**。"""
    case = make_case(root_cause_slice=None, required_depth="date_id", contribution_range=None)
    honest = make_conclusion(根因=None, 无法归因=True, 量级={"贡献度": None})
    dims = score_case(case, honest)["dimensions"]
    assert dims["定位命中"] is True and dims["深度达标"] is True and dims["数值准确"] is True
    # 攻击形态 1:不说无法归因(且给了根因)-> 定位不通过
    assert failed_dims(score_case(case, make_conclusion(无法归因=False))["dimensions"]) == {"定位命中"}
    # 攻击形态 2:说了无法归因却带着根因(想两边得分)-> 定位不通过
    sneaky = make_conclusion(无法归因=True, 量级={"贡献度": None},
                             根因={"dimension": "store", "level": "store_id", "key": "STORE_X"})
    assert score_case(case, sneaky)["dimensions"]["定位命中"] is False
    # 攻击形态 3:不说无法归因也不给根因
    silent = make_conclusion(无法归因=False, 根因=None, 量级={"贡献度": None})
    assert score_case(case, silent)["dimensions"]["定位命中"] is False
    # 空映射与 None 同判;带键的映射(哪怕值是 None)算「给了根因」——从严的一侧判
    empty = make_conclusion(无法归因=True, 根因={}, 量级={"贡献度": None})
    assert score_case(case, empty)["passed"] is True
    keyed = make_conclusion(无法归因=True, 根因={"key": None}, 量级={"贡献度": None})
    assert score_case(case, keyed)["dimensions"]["定位命中"] is False


@pytest.mark.parametrize("conclusion", [
    {}, {"结论": None}, {"根因": []}, {"根因": "文本"}, {"量级": "文本"}, {"量级": {"贡献度": "abc"}},
    {"已排除": None}, {"已排除": [{"a": 1}]}, {"置信度": 1}, {"无法归因": "yes"},
])
def test_score_case_never_raises_on_garbage(in_repo, conclusion):
    """评分器对模型输出完全容错:形状再怪也只返回 False,不许抛错。"""
    scored = score_case(make_case(), conclusion)
    assert set(scored["dimensions"]) == set(DIMS) and isinstance(scored["passed"], bool)
