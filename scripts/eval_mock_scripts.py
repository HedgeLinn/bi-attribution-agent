"""`evaluate_agent.py --mock` 的剧本(离线夹具,验证评分管线用)。

放在 `evaluate_agent.py` 之外的唯一原因:**单文件行数约束(≤300)**。
剧本不碰数据、不需要 BI_API_KEY:它只是按 case 的期望造一份「模型结论」,
好让评分管线(解析 -> 四维判定 -> 分组报告)在没有模型的机器上也走完整条路。

**它不是模型能力基准**:mock 报告的准确率只反映「剧本写得对不对」,与 agent 好坏无关。
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from scripts.eval_common import depth_levels

if TYPE_CHECKING:   # 仅类型标注用,避免运行时环:evaluate_agent -> 本模块 -> evaluate_agent
    from scripts.evaluate_agent import Case

# 剧本名(七种):全对 / 定位错 / 深度不够 / 数值出界 / 撞 must_not_claim /
#              三处同时错 / 结论不可解析
SCRIPT_CORRECT = "correct"
SCRIPT_LOCATION = "wrong_location"
SCRIPT_DEPTH = "shallow_depth"
SCRIPT_VALUE = "out_of_range"
SCRIPT_CLAIM = "forbidden_claim"
SCRIPT_MULTI = "multi_fail"
SCRIPT_UNPARSABLE = "unparseable"

# 出厂 case -> 剧本的**显式**绑定(id 是标识符,不是数据值)。
#
# 为什么不靠 crc32 轮换碰运气:剧本的**覆盖面本身是验收对象**——四维每一维都必须
# 至少有一个失败样本、也至少有一个通过样本,否则那一维在 mock 报告里是装饰品
# (把它的判定函数改成恒 True,报告逐字节不变)。轮换是均匀的,但不保证覆盖:
# 哪一维缺样本取决于 id 哈希,与 case 的期望内容毫无关系。
SCRIPTS_BY_ID = {
    "c1_store_cliff": SCRIPT_CORRECT,   # 全对:四维全过的通过样本
    "c2_sku_delist": SCRIPT_MULTI,      # 定位错 + 层级浅 + 贡献度出界:一条结论击中三个判定
    "c3_promo_noise": SCRIPT_CLAIM,     # 撞 must_not_claim + 无法归因 false
    "c4_seasonal": SCRIPT_CORRECT,      # 无法归因 true:诚实分支的通过样本
}

# 出厂 case 之外的**合成 id**(单元测试、M5 新增数据集)按 crc32 轮换六种经典剧本:
# 它保证任何 case 集都能走到六条判定路径,但不承诺「每一维都有失败样本」——那是上表的职责。
ROTATION = (SCRIPT_CORRECT, SCRIPT_LOCATION, SCRIPT_DEPTH,
            SCRIPT_VALUE, SCRIPT_CLAIM, SCRIPT_UNPARSABLE)

# mock 用的假成本:轮数 / tokens / 费用(记录项,不计分)
MOCK_USAGE = {"rounds": 5, "input_tokens": 18324, "output_tokens": 1266, "cost_cny": 0.07}


def pick_script(case_id: str) -> str:
    """该 case 用哪个剧本:出厂 case 显式绑定,其余合成 id 按 crc32 轮换六种。"""
    return SCRIPTS_BY_ID.get(case_id) or ROTATION[
        zlib.crc32(case_id.encode("utf-8")) % len(ROTATION)]


def mock_run(case: Case) -> tuple[str, dict]:
    """mock 模式的一步:返回该 case 的**脚本化**结论 (content, usage)。

    放在本模块(而不是 evaluate_agent.py)的原因:它就是「剧本」的出口,与剧本同生共死;
    `evaluate_agent.mock_run` 只是把它再导出。usage 给轮数 / tokens 的假值(记录项)。
    """
    return render_script(pick_script(case.id), case), dict(MOCK_USAGE)


def render_script(script: str, case: Case) -> str:
    """按剧本名渲染该 case 的结论内容(字符串);不可解析剧本返回一段非 JSON 文本。

    「全对」剧本套一层 ``` 围栏——让 mock 全链路也走到评分侧的围栏剥离路径。
    """
    if script == SCRIPT_UNPARSABLE:
        return "根据上述下钻过程,本次归因结论已在上方说明(未按 JSON 格式输出)。"
    expected = case.root_cause_slice
    conclusion = (unattributable_conclusion(script, case) if expected is None
                  else attributable_conclusion(script, case, expected))
    content = json.dumps(conclusion, ensure_ascii=False, indent=2)
    return f"```json\n{content}\n```" if script == SCRIPT_CORRECT else content


def attributable_conclusion(script: str, case: Case, expected: Mapping) -> dict:
    """有期望根因的 case:按剧本给「全对」或错在某一维(或几维)上的结论。

    错值一律从 case 自己的期望派生(`<key>-OTHER` / 该维度 hierarchy 的更浅一层 /
    区间上界之外),不写死任何实体值——剧本不认识数据集,只认识「怎么把答案弄错」。
    """
    key = str(expected.get("key"))
    root = {"dimension": expected.get("dimension"), "level": expected.get("level"),
            "key": key, "label": str(expected.get("label") or key)}
    if script in (SCRIPT_LOCATION, SCRIPT_MULTI):
        root = {**root, "key": f"{key}-OTHER", "label": f"{key}-OTHER"}
    if script in (SCRIPT_DEPTH, SCRIPT_MULTI):
        root = {**root, "level": shallower_level(root)}
    conclusion = {
        "结论": f"{root['label']} 的变化主导了本次指标波动。",
        "根因": root,
        "量级": {"贡献度": script_contribution(script, case), "变化量": -100000.0},
        "证据链": ["下钻到该层,该切片跌幅显著大于整体"],
        "已排除": ["渠道结构变化", "口径调整"],
        "无法归因": False,
        "置信度": "high",
    }
    if script == SCRIPT_CLAIM and case.must_not_claim:
        conclusion["已排除"] = [*conclusion["已排除"], case.must_not_claim[0]]
    return conclusion


def unattributable_conclusion(script: str, case: Case) -> dict:
    """无期望根因的 case(「无法归因」类):只有「全对」剧本承认无法归因。

    其余剧本硬报一个根因 —— 走的正是定位维的特别规则(case 无根因切片时,
    该维改判 conclusion["无法归因"] is True),这条路径必须被 mock 覆盖到。
    """
    conclusion: dict[str, Any] = {
        "结论": "未发现可归因的异常根因,属预期波动。",
        "根因": None,
        "量级": {"贡献度": None, "变化量": None},
        "证据链": ["逐维度下钻未发现显著偏离基线的切片"],
        "已排除": ["渠道结构变化", "口径调整"],
        "无法归因": True,
        "置信度": "high",
    }
    if script == SCRIPT_CORRECT:
        return conclusion
    conclusion["根因"] = {"dimension": "unknown", "level": case.required_depth,
                          "key": "SOME_SLICE", "label": "SOME_SLICE"}
    conclusion["无法归因"] = False
    conclusion["结论"] = "SOME_SLICE 的变化主导了本次指标波动。"
    if script == SCRIPT_CLAIM and case.must_not_claim:
        conclusion["已排除"] = [*conclusion["已排除"], case.must_not_claim[0]]
    return conclusion


def script_contribution(script: str, case: Case) -> float:
    """剧本用的贡献度:出界剧本取区间上界之外,其余取区间中值(无区间则 0.5)。"""
    bounds = case.contribution_range
    if script in (SCRIPT_VALUE, SCRIPT_MULTI):
        return round(bounds[1] + 0.5, 4) if bounds else 1.5
    return round((bounds[0] + bounds[1]) / 2, 4) if bounds else 0.5


def shallower_level(dimensions: Mapping) -> str:
    """比 required_depth 更浅的一层(取该维度 hierarchy 的第一层);取不到则给个假层名。"""
    hierarchy = depth_levels().get(str(dimensions.get("dimension") or ""))
    current = dimensions.get("level")
    candidates = [level for level in (hierarchy or ()) if level != current]
    return candidates[0] if candidates else "unknown_level"
