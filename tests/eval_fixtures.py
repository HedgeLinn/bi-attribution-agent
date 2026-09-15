"""评估脚手架测试的公共设施:case / 结论构造器、数据集包夹具、四维常量。

与既有惯例一致(`tests/*_fixtures.py`):这些夹具只服务评估相关的两个测试模块
(test_evaluate_agent.py 管解析与评分,test_eval_pipeline.py 管剧本与报告),
不参与 conftest.py 的数据集词汇收集。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.evaluate_agent import Case

REPO = Path(__file__).resolve().parents[1]
DATASET = REPO / "datasets" / "ecommerce-demo"
DIMS = ("定位命中", "深度达标", "数值准确", "无错误断言")

# 合成数据集包的最小语义层:深度维要读它(地图解析不到是抛错,不再是静默退化)
FAKE_SEMANTIC = ("fact_table: orders\ndate_field: date_id\n"
                 "metrics: {gmv: {expression: SUM(amount)}}\n"
                 "dimensions: {store: {key: store_id, hierarchy: [region, city, store_id]}}\n")

VALID_CASE = """
id: t1
tier: L2
category: 测试
question: "问?"
expected: {root_cause_slice: {dimension: store, level: store_id, key: STORE_X},
           mechanism: "机制", required_depth: store_id}
"""


@pytest.fixture(autouse=True)
def _cwd_repo(monkeypatch):
    """数据集 id 解析走的是相对路径(datasets/<id>):测试一律以仓库根为 CWD。"""
    monkeypatch.chdir(REPO)


@pytest.fixture()
def in_repo(monkeypatch):
    """钉住 BI_DATASET:score_case 签名冻结(没有数据集参数),深度维的地图只能从 env 取。"""
    monkeypatch.setenv("BI_DATASET", "ecommerce-demo")


def make_case(**over) -> Case:
    """评分测试用的 case(默认:门店层 STORE_X,区间 [0.3, 0.7])。"""
    base = dict(id="t1", tier="L2", category="测试", question="为什么?",
                root_cause_slice={"dimension": "store", "level": "store_id", "key": "STORE_X"},
                mechanism="机制", contribution_range=(0.3, 0.7), required_depth="store_id")
    return Case(**{**base, **over})


def make_conclusion(**over) -> dict:
    """「全对」的结论,再按测试需要覆盖字段(重复的键以 over 为准)。"""
    return {"结论": "STORE_X 主导了本次下滑", "量级": {"贡献度": 0.5}, "证据链": ["下钻"],
            "根因": {"dimension": "store", "level": "store_id", "key": "STORE_X"},
            "已排除": ["渠道结构变化"], "无法归因": False, "置信度": "high", **over}


def failed_dims(dimensions: dict) -> set[str]:
    """某次评分里没过的维度名集合(区分「哪一维抓到的」)。"""
    return {name for name, ok in dimensions.items() if not ok}


def write_case(directory: Path, name: str, body: str) -> None:
    """把一段 case YAML 写进 <directory>/<name>(目录不存在就建)。"""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")
