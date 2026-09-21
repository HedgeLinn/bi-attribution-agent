"""评估的运行、评分与报告:四维判定、跑一次真跑 loop、汇总成分组报告、写文件 / 打屏。

放在 `eval_common.py` / `evaluate_agent.py` 之外的唯一原因:**单文件行数约束(≤300)**。
分工:`evaluate_agent.py` 管「case 从哪来、要跑哪些、CLI 怎么进」;本模块管「一条结论
在这一维上算不算过」(四个单维判定 + 编排 `score_case`)与跑完之后的那三件事
(选哪些进报告、分组统计、打屏 / 写盘)。`run_live` 走延迟 import ——
mock 模式不装 langchain 也要能跑完整条评分管线。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.eval_common import (
    as_mapping, blob, depth_levels, strip_fences, to_number, try_json,
)

if TYPE_CHECKING:   # 仅类型标注用,避免运行时环:evaluate_agent -> 本模块 -> evaluate_agent
    from scripts.evaluate_agent import Case

# 四维的键名(报告、测试与看板都按这四个名字读,不得改名)
DIM_LOCATE = "定位命中"
DIM_DEPTH = "深度达标"
DIM_VALUE = "数值准确"
DIM_CLAIM = "无错误断言"

# 结论 JSON 里必须出现的字段(§5.8);缺字段 = 该字段对应的维不通过
_REQUIRED_FIELDS = ("结论", "根因", "量级", "证据链", "已排除", "无法归因", "置信度")
# 字段 -> 维的映射(「证据链」无对应维,缺了只记进 missing_fields,不扣分)
_MISSING_DIMS: dict[str, tuple[str, ...]] = {
    "结论": (DIM_CLAIM,), "根因": (DIM_LOCATE, DIM_DEPTH),
    "量级": (DIM_VALUE,), "已排除": (DIM_CLAIM,),
}
# 置信度的合法取值(§5.8);越界 = 没按格式回答,与不可解析同判
CONFIDENCE_VALUES = frozenset({"high", "medium", "low"})


def require_api_key() -> None:
    """真跑模式的前置条件:本机没有 Key 时给出可执行的提示,而不是等模型调用失败。

    两种协议任一凭证存在即放行:BI_API_KEY(OpenAI 协议)或
    ANTHROPIC_AUTH_TOKEN(Anthropic 协议)。"""
    has_openai = bool(os.environ.get("BI_API_KEY", "").strip())
    has_anthropic = bool(os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip())
    if not (has_openai or has_anthropic):
        raise RuntimeError(
            "真跑模式需要环境变量 BI_API_KEY 或 ANTHROPIC_AUTH_TOKEN(离线验证请加 --mock)")


def run_live(question: str) -> tuple[str, dict]:
    """真跑一个问题:harness.loop.run + 事件收集(延迟 import,离线路径不碰 LLM 依赖)。
    """
    from harness import loop   # noqa: PLC0415  延迟导入:mock 模式不需要 langchain

    events: list[dict] = []
    content = loop.run(question, verbose=False, on_event=events.append)
    return content, usage_from_events(events)


def usage_from_events(events: Sequence[dict]) -> dict:
    """从 loop 事件流里汇总成本记录:轮数取最大 step,用量取最后一次 usage 事件。"""
    rounds = 0
    usage = {"rounds": 0, "input_tokens": 0, "output_tokens": 0, "cost_cny": 0.0}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("type") in ("tool_call", "tool_result"):
            rounds = max(rounds, int(event.get("step") or 0))
        elif event.get("type") == "usage":
            usage = {
                "rounds": rounds,
                "input_tokens": int(event.get("input_tokens") or 0),
                "output_tokens": int(event.get("output_tokens") or 0),
                "cost_cny": float(event.get("cost_cny") or 0.0),
            }
    usage["rounds"] = rounds
    return usage


# ---------------------------------------------------------------------------
# 四维的单维判定:对**模型输出的形状**必须完全容错(只返回 bool,不许抛错)
#
# 唯一的例外是深度维要读语义层地图(eval_common.depth_levels):地图解析不到时它抛错,
# 那是环境 / 输入错误(数据集包不可解析),不是「模型输出怪」——静默退化会把更深的
# 正确答案判成不达标,那种假失败必须被看见。
# ---------------------------------------------------------------------------
def scorable(conclusion: dict | None) -> bool:
    """结论是否可作为评分输入:非映射(dict/None/其它)= 不可解析;置信度越界 = 没按格式回答。

    缺字段不走这条:缺哪个字段由**该字段对应的维**判不通过(见 _apply_missing)。
    """
    if not isinstance(conclusion, Mapping):
        return False
    confidence = conclusion.get("置信度")
    return confidence is None or confidence in CONFIDENCE_VALUES


def locate_ok(case: Case, root: Mapping) -> bool:
    """定位命中(有期望根因的 case):根因 key 与期望切片 key 一致。

    数字型 key 按文本比较,其余严格比较;根因缺失(空映射)时 key 取不到,判 False。
    """
    key, expected = root.get("key"), case.root_cause_slice.get("key")
    return key is not None and str(key) == str(expected)


def depth_ok(case: Case, root: Mapping) -> bool:
    """深度达标:根因 level 在维度 hierarchy 里的位置 >= required_depth 的位置。

    该维度不在地图里(语义层没声明这一维的层级)时退化为「同层才算达标」;
    整张地图读不到是另一回事——那种情况在 depth_levels() 里就抛错了。
    """
    level = root.get("level")
    if not isinstance(level, str) or not level.strip():
        return False
    hierarchy = depth_levels().get(str(root.get("dimension") or ""))
    required = case.required_depth
    if hierarchy and level in hierarchy and required in hierarchy:
        return hierarchy.index(level) >= hierarchy.index(required)
    return level == required


def value_ok(case: Case, conclusion: Mapping) -> bool:
    """数值准确:量级.贡献度 ∈ contribution_range(闭区间)。

    case 没声明区间 -> 本维跳过(视为通过,「case 没要求就不评」)。
    声明了区间却没给可用的贡献度(量级缺失 / 贡献度为 None / 非数值)-> 不通过:
    §5.8 的必填字段必须被强制,否则「不写就不会错」。
    """
    bounds = case.contribution_range
    if bounds is None:
        return True
    value = to_number(as_mapping(conclusion.get("量级")).get("贡献度"))
    return value is not None and bounds[0] <= value <= bounds[1]


def claim_ok(case: Case, conclusion: Mapping) -> bool:
    """无错误断言:must_not_claim 的任一短语出现在「结论」或「已排除」文本里即判错。"""
    text = "\n".join((blob(conclusion.get("结论")), blob(conclusion.get("已排除"))))
    return not any(phrase in text for phrase in case.must_not_claim)


def score_case(case: Case, conclusion: dict | None, usage: dict | None = None) -> dict:
    """对单个 case 做四维机械评分,返回 {case_id, passed, dimensions, usage, missing_fields}。

    dimensions = {定位命中 / 深度达标 / 数值准确 / 无错误断言: bool};passed = 全部 True。
    conclusion 为 None(不可解析)时四维全 False。§5.8 的必填字段缺一个就按 _MISSING_DIMS
    扣对应维;「无法归因」与「根因」互斥、对两类 case 双向生效——null-root case 嘴上说
    无法归因手里还塞根因,定位维不通过(诚实分支不是免检通道);有期望根因的 case 宣布
    无法归因却给出根因,定位/深度两维都不作数(矛盾答案不是「顺便全对」)。任何输入都不许
    抛错——评分器对「模型输出了什么」必须完全容错(深度维例外:语义层地图解析不到时抛错,
    那是环境 / 输入错误)。
    """
    dimensions = {DIM_LOCATE: False, DIM_DEPTH: False, DIM_VALUE: False, DIM_CLAIM: False}
    missing = _missing_fields(conclusion)
    if scorable(conclusion):
        root = as_mapping(conclusion.get("根因"))
        if case.root_cause_slice is None:
            # 「无法归因」类 case:定位维改判诚实度;无根因可判 -> 深度维跳过(视为通过)
            dimensions[DIM_LOCATE] = conclusion.get("无法归因") is True and not root
            dimensions[DIM_DEPTH] = True
        else:
            dimensions[DIM_LOCATE] = locate_ok(case, root)
            dimensions[DIM_DEPTH] = depth_ok(case, root)
            if conclusion.get("无法归因") is True and root:
                # 有期望根因却宣布「无法归因」还塞了根因:自相矛盾,定位/深度都不作数
                # (互斥规则对两类 case 双向生效,不只是 null-root case 的免检通道)
                dimensions[DIM_LOCATE] = False
                dimensions[DIM_DEPTH] = False
        dimensions[DIM_VALUE] = value_ok(case, conclusion)
        dimensions[DIM_CLAIM] = claim_ok(case, conclusion)
    _apply_missing(dimensions, missing, case)
    return {"case_id": case.id, "passed": all(dimensions.values()),
            "dimensions": dimensions, "usage": usage or {}, "missing_fields": missing}


def _missing_fields(conclusion: dict | None) -> list[str]:
    """§5.8 的必填字段里缺了哪些(顺序按 _REQUIRED_FIELDS)。

    整份结论不可解析(非映射)不算「缺字段」:那是「没按格式回答」,四维已经全 False。
    """
    if not isinstance(conclusion, Mapping):
        return []
    return [field for field in _REQUIRED_FIELDS if field not in conclusion]


def _apply_missing(dimensions: dict, missing: Sequence[str], case: Case) -> None:
    """缺字段 -> 对应维不通过。两条例外都属于「case 没要求就不评」那一类:

      - 「量级」:case 没声明 contribution_range 时数值维本就跳过,缺了不扣;
      - 「根因」:null-root case 的根因本就该为空,缺字段与写 null 等价(互斥规则另有判定)。
    """
    for field in missing:
        if field == "量级" and case.contribution_range is None:
            continue
        if field == "根因" and case.root_cause_slice is None:
            continue
        for dim in _MISSING_DIMS.get(field, ()):
            dimensions[dim] = False
    if "无法归因" in missing and case.root_cause_slice is None:
        dimensions[DIM_LOCATE] = False     # null-root case 的定位维全靠它,缺了不算诚实


def parse_conclusion(content: str) -> dict | None:
    """最终结论字符串 -> dict(围栏剥离 + 首尾花括号回退);失败返回 None。

    与 harness.loop._parse_conclusion 同一套口径(评分侧不依赖 harness,故自带一份)。
    """
    if not content:
        return None
    for candidate in (strip_fences(content), content):
        parsed = try_json(candidate)
        if parsed is not None:
            return parsed
    text = strip_fences(content)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return try_json(text[start:end + 1])
    return None


def case_record(case: Case, content: str, usage: dict) -> dict:
    """单个 case 的报告条目:评分结果 + 记录项 + 解析后的结论 + 缺失字段(便于人工复核)。"""
    conclusion = parse_conclusion(content)
    scored = score_case(case, conclusion, usage)
    return {"id": case.id, "tier": case.tier, "category": case.category,
            "passed": scored["passed"], "dimensions": scored["dimensions"],
            "missing_fields": scored["missing_fields"], "usage": scored["usage"],
            "conclusion": conclusion}


# ---------------------------------------------------------------------------
# 汇总与输出
# ---------------------------------------------------------------------------
def accuracy(passed: int, total: int) -> float:
    """准确率:全过的 case 占比;无 case 时为 0。"""
    return round(passed / total, 4) if total else 0.0


def group(records: Sequence[Mapping], field: str) -> dict:
    """按记录里的某个字段分组统计 {取值: {total, passed, accuracy}}(键排序,结果可复现)。"""
    grouped: dict[str, dict] = {}
    for record in records:
        bucket = grouped.setdefault(str(record[field]), {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += int(bool(record["passed"]))
    for bucket in grouped.values():
        bucket["accuracy"] = accuracy(bucket["passed"], bucket["total"])
    return {name: grouped[name] for name in sorted(grouped)}


def print_eval_report(report: Mapping) -> None:
    """评估报告的人读概要:过滤 / 截断声明 + 总计 + 分组 + 逐 case 的维度明细。

    「分母是多少」必须自证:下面的准确率是**过滤 + 截断之后**那个集合的准确率,
    不写出来就会被当成全量准确率读。
    """
    print(f"数据集 {report['dataset']}(v{report['dataset_version']}) · {report['mode']} 模式")
    active = _active_filters(report.get("filters"))
    if active:
        print(f"过滤:{active}(分母 = 过滤后的集合)")
    if report.get("limit") is not None:
        print(f"已按 --limit {report['limit']} 截断(分母 = 截断后的集合)")
    print(f"总计 {report['total']} case,通过 {report['passed']},准确率 {report['accuracy']:.2%}")
    for title, field in (("按分类", "by_category"), ("按难度", "by_tier")):
        for name, bucket in report[field].items():
            print(f"  {title} {name}: {bucket['passed']}/{bucket['total']}"
                  f"({bucket['accuracy']:.2%})")
    for record in report["cases"]:
        failed = [name for name, ok in record["dimensions"].items() if not ok]
        detail = "全维通过" if not failed else "未过:" + "、".join(failed)
        print(f"  [{'PASS' if record['passed'] else 'FAIL'}] {record['id']}"
              f"({record['tier']}/{record['category']}) {detail}")


def _active_filters(filters: Mapping | None) -> str:
    """把报告的 filters 渲染成 `case=a,b; tier=L2` 这类一行文本;没有生效的过滤返回 ""。"""
    if not isinstance(filters, Mapping):
        return ""
    parts = [f"{name}={','.join(map(str, values))}"
             for name, values in filters.items() if values]
    return "; ".join(parts)


def write_report(path: str, report: Mapping) -> None:
    """把报告写成 UTF-8 JSON(中文不转义,便于直接看)。"""
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
