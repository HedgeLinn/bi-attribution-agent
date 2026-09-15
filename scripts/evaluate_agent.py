"""评估脚手架(M4):跑 case、四维机械评分、出分组准确率报告。

契约:docs/REUSE_DESIGN.md §5.7(case 定义)/ §5.8(结构化结论)/ §5.9(评分)

用法:
    python scripts/evaluate_agent.py --dataset ecommerce-demo              # 真跑(需 BI_API_KEY)
    python scripts/evaluate_agent.py --dataset ecommerce-demo --mock       # 离线:脚本化结论,验证评分管线
    python scripts/evaluate_agent.py --dataset ecommerce-demo --tier L3 --limit 2 --out report.json
    python scripts/evaluate_agent.py --dataset ecommerce-demo --case c1_store_cliff --category 零售
    python scripts/evaluate_agent.py --dataset ecommerce-demo --sample 8 --mock   # 随机抽 8 条(固定种子)

case 来源:`datasets/<name>/cases/*.yaml`(schema 见 §5.7;每个文件一个 case,
键:id / tier / category / question / expected / distractors / slice_occupancy)。

评分四维(机械可判,不用人看):
    定位命中   conclusion["根因"]["key"] == expected.root_cause_slice.key
    深度达标   根因 level 在维度 hierarchy 里的位置 >= required_depth 的位置
    数值准确   量级.贡献度 ∈ contribution_range(case 没声明区间时跳过,不算失败)
    无错误断言 结论与已排除都不出现 must_not_claim 的任一短语
准确率 = 四维全过(跳过维视为过)的 case 数 / 总数,按 category 与 tier 分组报告。
成本(轮数 / tokens / 耗时)是记录项,不计分。
§5.8 的七个必填字段缺一个就按「该字段对应的维不通过」处理(见 eval_report._MISSING_DIMS);
「无法归因」与「根因」互斥、对两类 case 双向生效——null-root case 嘴上说无法归因、手里还塞
根因,定位维不通过;有期望根因的 case 宣布无法归因却带上根因,定位/深度两维都不作数。

模块分工(单文件 ≤300 行的产物;入口与公共名字仍全部从本模块导出):
    eval_common.py       读 YAML、解析数据集包、深度维的语义层地图(纯基础设施)
    eval_report.py       四维判定 + 评分编排 score_case + 过滤 select_cases + 报告
    eval_mock_scripts.py mock 剧本与 mock_run(离线夹具,不是模型能力基准)
    verify_dataset.py    数据集埋点回验(§5.10 约束 4),与本脚本共用上面两个模块

⚠️ mock 模式只用于验证**评分管线**(解析 -> 评分 -> 分组),它的「准确率」没有任何
模型能力含义;每 case 的 mock 结论由 mock 剧本给出(见 scripts/eval_mock_scripts.py)。
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 直接 `python scripts/evaluate_agent.py` 运行时,项目根不在搜索路径上,先补上
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.datasets import DatasetError  # noqa: E402
from scripts.eval_common import (  # noqa: E402
    EXIT_FAILED, EXIT_OK, build_case, dataset_package, dataset_version, fail,
)
# 以下名字在本模块「只是再导出」:测试与文档都按 scripts.evaluate_agent.<name> 取用
from scripts.eval_mock_scripts import MOCK_USAGE, mock_run, pick_script, render_script  # noqa: E402, F401
from scripts.eval_report import (  # noqa: E402
    accuracy, case_record, group, parse_conclusion, print_eval_report,  # noqa: F401
    require_api_key, run_live, score_case, write_report,
)

__all__ = ["Case", "evaluate", "load_cases", "main", "mock_run", "parse_conclusion",
           "score_case", "select_cases"]

_CASES_DIRNAME = "cases"
_YAML_SUFFIX = ".yaml"

# --sample 的随机种子:固定值保证「同命令 -> 同 case 集 -> 同报告」(REUSE_DESIGN §7③
# 提过抽样会引入不确定性;固定种子让抽样保留随机性又不破坏结果可复现原则)
SAMPLE_SEED = 0


@dataclass(frozen=True)
class Case:
    """一条评估 case(cases/*.yaml 解析后的形状;字段名与 §5.7 对齐)。"""

    id: str
    tier: str                      # L1 ~ L4
    category: str                  # 分组报告用
    question: str                  # 用户问题(跑 harness loop 的入参)
    root_cause_slice: dict         # {dimension, level, key}(label 可选)
    mechanism: str                 # 期望的机制描述(报告里展示,不参与评分)
    contribution_range: tuple[float, float] | None   # [下限, 上限],None = 不评数值维
    required_depth: str            # 维度层级字段名,根因 level 必须到这一层或更深
    must_not_claim: tuple[str, ...] = ()   # 出现在结论/已排除里即判错
    distractors: tuple[str, ...] = ()      # 干扰项(文档用途,不参与评分)
    raw: dict[str, Any] | None = None      # YAML 原文(扩展字段走这里)


def load_cases(dataset_dir: str) -> list[Case]:
    """读 datasets/<name>/cases/*.yaml -> 按文件名排序的 Case 列表。

    解析失败(缺字段 / YAML 非法)直接抛错——case 是评估的事实来源,坏 case 必须显式失败,
    不许静默跳过(否则准确率的分母会被偷偷缩小)。目录里一个 case 都没有同样抛错:
    空 case 集会产出 0/0 的「全绿」报告,比坏 case 更危险。
    """
    cases_dir = Path(dataset_dir) / _CASES_DIRNAME
    if not cases_dir.is_dir():
        raise FileNotFoundError(f"case 目录不存在:{cases_dir}(每个 case 一个 YAML)")
    paths = sorted(cases_dir.glob(f"*{_YAML_SUFFIX}"))
    if not paths:
        raise ValueError(f"case 目录里没有任何 *{_YAML_SUFFIX} 文件:{cases_dir}"
                         "(0 分母的报告会显示「全绿」,不许产出)")
    return [Case(**build_case(path)) for path in paths]


def select_cases(all_cases: Sequence[Case], *, case_ids: Sequence[str] | None = None,
                 categories: Sequence[str] | None = None,
                 tiers: Sequence[str] | None = None) -> tuple[list[Case], dict]:
    """按 --case / --category / --tier 过滤 case 集 -> (过滤后的 case, 报告用的 filters)。

    三类条件之间是**与**,同类内部是**或**(多个 --case 取并集)。指定的 case id 有
    不认识的 -> 抛错(敲错 id 却照样产出报告,是最坏的一种「绿」);过滤后一个 case
    都不剩 -> 同样抛错(0 分母的准确率没有意义,与空 case 目录同立场)。
    """
    wanted = {"case": _unique(case_ids), "category": _unique(categories),
              "tier": _unique(tiers)}
    known = {case.id for case in all_cases}
    unknown = [value for value in wanted["case"] if value not in known]
    if unknown:
        raise ValueError(f"指定的 case 不存在:{'、'.join(unknown)}"
                         f"(本数据集可用 id:{'、'.join(sorted(known)) or '无'})")
    ids, cats, names = set(wanted["case"]), set(wanted["category"]), set(wanted["tier"])
    selected = [case for case in all_cases
                if (not ids or case.id in ids) and (not cats or case.category in cats)
                and (not names or case.tier in names)]
    if not selected:
        raise ValueError(f"过滤后没有 case 可评(filters={wanted}):"
                         "0 分母的报告会显示「全绿」,不许产出")
    return selected, wanted


def _unique(values: Sequence[str] | None) -> list[str]:
    """去重后排序:报告里的 filters 要能一眼看出筛了什么,且结果可复现。"""
    return sorted(set(values or ()))


def _sampled(cases: Sequence[Case], size: int) -> list[Case]:
    """按固定种子随机抽 size 个 case;size 不小于总数时抽全部(顺序不变)。

    输入顺序已确定(load_cases 按文件名排序、select_cases 保序),种子固定,
    所以「同一份 case 集 + 同一 size」抽到的子集恒相同。
    """
    if size >= len(cases):
        return list(cases)
    return random.Random(SAMPLE_SEED).sample(list(cases), size)


def evaluate(dataset_dir: str, *, mock: bool = False, limit: int | None = None,
             sample: int | None = None, case_ids: Sequence[str] | None = None,
             categories: Sequence[str] | None = None,
             tiers: Sequence[str] | None = None) -> dict:
    """跑(过滤后的) case -> 评分 -> 分组报告,返回可直接 json.dumps 的报告 dict。

    报告结构:
      {dataset, dataset_version, mode, limit, filters, total, passed, accuracy,
       by_category: {分类: {total, passed, accuracy}},
       by_tier: {tier: {total, passed, accuracy}},
       cases: [{id, tier, category, passed, dimensions, missing_fields, usage, conclusion}]}
    `limit` 与 `filters` 是**分母的自证**:准确率是过滤 → 抽样 → 截断之后那个集合的
    准确率(limit / sample 为 None = 该步没生效;filters 里某项为空列表 = 该条件没生效)。
    顺序:select_cases(过滤) -> sample(固定种子随机抽样) -> limit(截断)。
    过滤条件筛不出 case、指定的 case id 不存在、limit/sample 非正 -> 抛 ValueError。
    真跑模式(mock=False)需要 BI_API_KEY;每 case 跑一次 harness.loop.run,
    把 (question, final 内容, usage 事件) 喂给 score_case。
    """
    info = dataset_package(dataset_dir)
    if limit is not None and limit <= 0:
        raise ValueError(f"--limit 必须为正整数(实际 {limit}):截断到 0 个 case 没有意义")
    if sample is not None and sample <= 0:
        raise ValueError(f"--sample 必须为正整数(实际 {sample}):抽 0 个 case 没有意义")
    cases, filters = select_cases(load_cases(str(info.root)), case_ids=case_ids,
                                  categories=categories, tiers=tiers)
    if sample is not None:
        cases = _sampled(cases, sample)
        filters["sample"] = [str(sample)]     # 分母自证:抽样口径写进报告
    if limit is not None:
        cases = cases[:limit]
    if not mock:
        require_api_key()
        # loop.run 会用 tools 的全局引擎 / 语义层单例;CLI 与前端都先 init_engine,
        # 评估这条路径直接进 loop,必须同样注入(延迟 import:mock 路径不碰 LLM 依赖)
        from harness import tools  # noqa: PLC0415

        tools.init_engine(str(info.data_dir), str(info.semantic_path))
    records = []
    # 评分期的深度维要按**被评估数据集**的语义层解析层级;score_case 的签名已冻结
    # (没有数据集参数),只能借 BI_DATASET 传递上下文。写进去的是数据集包的**绝对根目录**
    # 而不是 id:数据集包可以在仓库外(--dataset <目录>),用 id 会按 CWD 去 datasets/ 找,
    # 找不到时深度维就失去了地图(更深的正确答案会被判成不达标)。跑完还原,不污染调用方。
    previous_env = os.environ.get("BI_DATASET")
    os.environ["BI_DATASET"] = str(info.root.resolve())
    try:
        for case in cases:
            content, usage = mock_run(case) if mock else run_live(case.question)
            records.append(case_record(case, content, usage))
    finally:
        if previous_env is None:
            os.environ.pop("BI_DATASET", None)
        else:
            os.environ["BI_DATASET"] = previous_env
    passed = sum(1 for record in records if record["passed"])
    return {
        "dataset": info.id,
        "dataset_version": dataset_version(info),
        "mode": "mock" if mock else "live",
        "limit": limit,
        "filters": filters,
        "total": len(records),
        "passed": passed,
        "accuracy": accuracy(passed, len(records)),
        "by_category": group(records, "category"),
        "by_tier": group(records, "tier"),
        "cases": records,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:--dataset 必填;--mock / --limit / --sample / --case / --category / --tier / --out 可选。

    退出码:0 报告已产出 / 1 运行失败(缺 BI_API_KEY、loop 或引擎报错) / 2 用法错误。
    """
    parser = argparse.ArgumentParser(
        description="归因 agent 评估脚手架(docs/REUSE_DESIGN.md §5.7-5.9)")
    parser.add_argument("--dataset", metavar="id|目录", help="数据集 id 或数据集包目录")
    parser.add_argument("--mock", action="store_true",
                        help="离线模式:用脚本化结论验证评分管线(准确率无模型能力含义)")
    parser.add_argument("--limit", type=_positive_int, metavar="N", help="只跑前 N 个 case")
    parser.add_argument("--sample", type=_positive_int, metavar="N",
                        help="从过滤后的 case 里随机抽 N 个(固定种子,同命令同结果)")
    parser.add_argument("--case", dest="case_ids", action="append", metavar="ID",
                        help="只跑这些 id 的 case(可多次;多个 id 取并集)")
    parser.add_argument("--category", dest="categories", action="append", metavar="分类",
                        help="只跑这些分类的 case(可多次)")
    parser.add_argument("--tier", dest="tiers", action="append", metavar="级别",
                        help="只跑这些难度的 case(可多次,如 L2)")
    parser.add_argument("--out", metavar="路径", help="把 JSON 报告写到该文件")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:   # 参数非法:argparse 退出码 2;--help 退出码 0 原样透传
        return int(exc.code or EXIT_OK)
    if not args.dataset:
        return fail("需要 --dataset <id>(或数据集包目录)")
    try:
        report = evaluate(args.dataset, mock=args.mock, limit=args.limit,
                          sample=args.sample, case_ids=args.case_ids,
                          categories=args.categories, tiers=args.tiers)
    except (DatasetError, FileNotFoundError, ValueError, OSError) as err:
        return fail(str(err))
    except Exception as err:   # noqa: BLE001  真跑分支的运行期失败(无 Key / 模型 / 引擎)
        return fail(f"评估失败:{err}", code=EXIT_FAILED)
    print_eval_report(report)
    if args.out:
        try:
            write_report(args.out, report)
        except OSError as err:
            return fail(f"报告写入失败:{err}", code=EXIT_FAILED)
        print(f"报告已写入:{args.out}")
    return EXIT_OK


def _positive_int(text: str) -> int:
    """argparse 的 --limit 取值:必须是正整数(0 / 负数会让「分母」失去意义)。"""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"应为整数(实际 {text!r})") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"应为正整数(实际 {value})")
    return value


if __name__ == "__main__":
    sys.exit(main())
