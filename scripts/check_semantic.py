# -*- coding: utf-8 -*-
"""语义层治理入口:对比两版地图 / 校验当前地图(docs/REUSE_DESIGN.md §3.6②③)。

用法:

    python scripts/check_semantic.py --old <旧语义层> --new <新语义层>   # 变更检测
    python scripts/check_semantic.py --dataset <id>                      # 仅校验当前语义层

破坏性变更(改口径 / 删指标或维度 / 改 hierarchy 顺序 / 改 time_aggregation)会让
历史数值不可比、让 case 的期望值失效,必须走完整治理流程:bump dataset_version +
重算受影响 case。**这件事靠人记不住,所以退出码要能被 CI 读到**:

    0  无变更 / 仅增量变更 / 当前语义层校验通过
    1  检测到破坏性变更,或当前语义层校验未通过
    2  用法或输入错误(参数不完整、文件读不到、YAML 非法、数据集无法解析)

判定规则本身住 attribution/semantic_diff.py(级别表与理由见其模块文档)。
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

# 直接 `python scripts/check_semantic.py` 运行时,项目根不在搜索路径上,先补上
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from attribution.semantic import Semantic, SemanticError  # noqa: E402
from attribution.sql_source import introspect_columns  # noqa: E402
from attribution.semantic_diff import (  # noqa: E402
    LEVEL_BREAKING, LEVEL_INCREMENTAL, Change, diff_layers, has_breaking,
)
from harness.datasets import DatasetError, resolve_dataset  # noqa: E402

# 退出码(见模块文档):调用方据此判断是否放行
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INPUT = 2

# 级别 -> 输出标签
_LEVEL_TAGS = {LEVEL_BREAKING: "[破坏性]", LEVEL_INCREMENTAL: "[增量]"}

# 破坏性变更后必须做的事(§3.6①:评估结果绑定 dataset_version)
_MUST_DO = ("检测到破坏性变更——合并前必须:① bump dataset_version;"
            "② 重算受影响 case(期望的贡献区间 / required_depth 已随口径或下钻路径失效)。")

# 数据集包定位:语义层同级(或上一级)的 dataset.yaml 标出包根
_MANIFEST_NAME = "dataset.yaml"
_CASES_DIRNAME = "cases"


def main(argv: Sequence[str] | None = None) -> int:
    """命令入口:返回退出码(不直接 sys.exit,便于测试与复用)。"""
    args = _parse_args(argv)
    if args.dataset:
        if args.old or args.new:
            return _fail("--dataset 与 --old / --new 不能同时使用")
        return _check_dataset(args.dataset)
    if not (args.old and args.new):
        return _fail("需要同时给出 --old 与 --new(或用 --dataset 只校验当前语义层)")
    return _diff_layers(args.old, args.new)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """解析命令行参数;参数本身不做必填约束(缺参由 main 给退出码 2)。"""
    parser = argparse.ArgumentParser(
        description="语义层变更检测与校验(docs/REUSE_DESIGN.md §3.6②③)")
    parser.add_argument("--old", metavar="路径", help="旧语义层 YAML")
    parser.add_argument("--new", metavar="路径", help="新语义层 YAML")
    parser.add_argument("--dataset", metavar="id", help="数据集 id:仅校验当前语义层")
    return parser.parse_args(argv)


# ----------------------------------------------------------------------
# 变更检测
# ----------------------------------------------------------------------
def _diff_layers(old_path: str, new_path: str) -> int:
    """对比两版语义层并报告;返回退出码。"""
    try:
        changes = diff_layers(old_path, new_path)
    except SemanticError as err:
        return _fail(f"语义层加载失败:{err}")
    return _report_changes(changes, _dataset_root_for(new_path))


def _report_changes(changes: Sequence[Change], dataset_root: Path | None = None) -> int:
    """逐条打印变更(破坏性在前);末尾给出必须做的事,并返回退出码。

    dataset_root:新语义层所属的数据集包根目录(定位不到则为 None)。破坏性变更时,
    影响分析要落到具体 case(REUSE_DESIGN §3.6:列出受影响对象,而不是
    只泛泛说「重算受影响 case」);定位不到包时降级为提示,不影响退出码。
    """
    if not changes:
        print("无变更:两版语义层完全一致。")
        return EXIT_OK
    breaking = [change for change in changes if change.level == LEVEL_BREAKING]
    print(f"共 {len(changes)} 条变更:破坏性 {len(breaking)} 条、"
          f"增量 {len(changes) - len(breaking)} 条")
    for change in changes:
        tag = _LEVEL_TAGS.get(change.level, change.level)
        print(f"  {tag} ({change.kind}) {change.path}")
        print(f"      {change.detail}")
    if not has_breaking(changes):
        print("仅增量变更,可直接合入(docs/REUSE_DESIGN.md §3.6②)。")
        return EXIT_OK
    _print_impact(dataset_root)
    print(_MUST_DO)
    return EXIT_FAILED


def _dataset_root_for(semantic_path: str) -> Path | None:
    """从语义层路径定位数据集包根目录:同级或上一级的 dataset.yaml。

    兼容两种布局:当前 ``datasets/<id>/semantic.yaml`` 的同级,以及迁移前的
    ``<root>/semantic/semantic.yaml`` 的上一级。定位不到返回 None(调用方降级为提示)。
    """
    path = Path(semantic_path)
    for directory in (path.parent, path.parent.parent):
        if (directory / _MANIFEST_NAME).is_file():
            return directory
    return None


def _affected_cases(dataset_root: Path) -> list[str]:
    """数据集包 cases/ 目录下的 case id(文件名去后缀,按名排序)。"""
    cases_dir = dataset_root / _CASES_DIRNAME
    if not cases_dir.is_dir():
        return []
    return sorted(path.stem for path in cases_dir.glob("*.yaml"))


def _print_impact(dataset_root: Path | None) -> None:
    """破坏性变更的影响分析:列出须重算的 case。"""
    if dataset_root is None:
        print("提示:未能定位数据集包(语义层同级或上一级无 dataset.yaml),"
              "无法列出受影响 case——请把语义层放进 datasets/<id>/ 布局。")
        return
    cases = _affected_cases(dataset_root)
    if cases:
        print(f"受影响 case({len(cases)} 个,须重算贡献区间 / required_depth):{'、'.join(cases)}")
    else:
        print(f"该数据集包尚无 {_CASES_DIRNAME}/ 目录或无 case 文件({dataset_root / _CASES_DIRNAME})。")


# ----------------------------------------------------------------------
# 当前语义层校验
# ----------------------------------------------------------------------
def _check_dataset(dataset_id: str) -> int:
    """按数据集 id 取语义层路径,做自洽性(§3.5)+ 可达性(§3.6④)校验。"""
    try:
        info = resolve_dataset(dataset_id)
    except NotImplementedError as err:   # harness/datasets 尚未落地时的兜底提示
        return _fail(f"数据集解析不可用({err});请改用 --old / --new 指定语义层")
    except DatasetError as err:
        return _fail(f"数据集 {dataset_id} 无法解析:{err}")

    print(f"数据集:{info.id}({info.root})")
    semantic = _load_semantic(str(info.semantic_path), str(info.data_dir))
    problems = semantic.validate() + semantic.check_reachability()
    if problems:
        print(f"语义层校验未通过({len(problems)} 个问题):")
        for problem in problems:
            print(f"  - {problem}")
        return EXIT_FAILED
    print(f"语义层校验通过:{info.semantic_path}")
    return EXIT_OK


def _load_semantic(semantic_path: str, data_dir: str) -> Semantic:
    """加载语义层;能读到数据列信息时一并做可达性校验,读不到则明确提示后降级。"""
    try:
        columns = introspect_columns(data_dir)
    except Exception as err:   # 数据缺失/损坏不该让「校验地图」这件事整个失败
        print(f"提示:读不到 {data_dir} 的列信息({err}),本次跳过可达性校验(§3.6④)")
        return Semantic.load(semantic_path)
    return Semantic.load(semantic_path, columns=columns)


def _fail(message: str) -> int:
    """输入/用法错误:打到 stderr 并返回退出码 2(不抛 SystemExit,便于测试)。"""
    print(f"错误:{message}", file=sys.stderr)
    return EXIT_INPUT


if __name__ == "__main__":   # pragma: no cover - 进程入口
    sys.exit(main())
