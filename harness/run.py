"""harness 的入口:解析数据集、初始化引擎、跑归因分析。

用法:
    python harness/run.py "为什么 2026 年 6 月 GMV 下滑了?"
    python harness/run.py --dataset ecommerce-demo "为什么 2026 年 6 月 GMV 下滑了?"
    python harness/run.py --data-dir D:\\mydata --semantic D:\\mydata\\semantic.yaml "查一下异常"
    python harness/run.py --list-datasets

数据集统一由 harness.datasets.resolve_dataset 解析(优先级见其 docstring):
什么都不传时,磁盘上恰好只有一个数据集就用它,零配置即可跑。
"""
import argparse
import os
import sys

# 把项目根目录加入 sys.path,保证 `from harness import ...` 可用
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import tools  # noqa: E402
from harness.datasets import (  # noqa: E402
    DEFAULT_DATASETS_DIRNAME,
    ENV_DATASET_ID,
    MANIFEST_NAME,
    DatasetError,
    DatasetInfo,
    discover_datasets,
    resolve_dataset,
)
from harness.loop import run  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """命令行参数:位置参数是分析问题,其余用于选择数据集。"""
    parser = argparse.ArgumentParser(
        description="BI 归因分析 agent(换数据 = 换数据集目录,docs/REUSE_DESIGN.md §3.3)",
    )
    parser.add_argument("query", nargs="?", help="你的分析问题")
    parser.add_argument("--dataset", metavar="ID",
                        help=f"数据集 id(等价环境变量 {ENV_DATASET_ID})")
    parser.add_argument("--data-dir", dest="data_dir", metavar="DIR",
                        help="自带数据目录,须与 --semantic 同时给出")
    parser.add_argument("--semantic", metavar="FILE",
                        help="自带语义层文件,须与 --data-dir 同时给出")
    parser.add_argument("--list-datasets", dest="list_datasets", action="store_true",
                        help="列出可用数据集后退出(不需要 API Key)")
    return parser


def list_datasets() -> int:
    """打印可用数据集(id / title / version)。返回码 0——「还没有数据集」是合法状态。"""
    found = discover_datasets()
    if not found:
        print(f"未发现任何数据集:{DEFAULT_DATASETS_DIRNAME}/ 下没有带 {MANIFEST_NAME} 的目录。")
        return 0
    print(f"可用数据集({len(found)} 个,来自 {DEFAULT_DATASETS_DIRNAME}/):")
    for info in found:
        print(f"  {info.id} · {info.title} · v{info.version or '未标注版本'}")
    return 0


def resolve_or_report(args: argparse.Namespace) -> DatasetInfo | None:
    """解析数据集;失败时打印原因并返回 None(错误信息里已含可选 id)。"""
    try:
        return resolve_dataset(args.dataset, args.data_dir, args.semantic)
    except DatasetError as exc:
        print(f"数据集解析失败:{exc}")
        return None


def main() -> int:
    # 统一 stdout 编码,避免 Windows 控制台中文乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = build_parser()
    args = parser.parse_args()

    if args.list_datasets:
        return list_datasets()

    if not args.query:
        parser.print_help()
        return 1

    dataset = resolve_or_report(args)
    if dataset is None:
        return 1
    print(f"数据集:{dataset.id}({dataset.title})· 数据目录 {dataset.data_dir}"
          f" · 语义层 {dataset.semantic_path}")

    # 1. 初始化归因引擎 + 语义层
    tools.init_engine(str(dataset.data_dir), str(dataset.semantic_path))

    # 2. 跑归因 loop
    result = run(args.query, verbose=True)

    print("\n" + "=" * 60)
    print("最终结论:")
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
