"""数据集埋点回验(M4):独立脚本反向检测「每个坑是否可观测、幅度是否匹配声明」。

契约:docs/REUSE_DESIGN.md §5.10 约束 4(可回验)

用法:
    python scripts/verify_dataset.py --dataset ecommerce-demo

期望来源:`datasets/<name>/expectations.yaml`。schema(冻结):

    dataset_version: "1.0.0"          # 回验结果绑定该版本(与语义层 dataset_version 对齐)
    expectations:
      - id: store_cliff               # 唯一标识,报告里引用
        description: 上海徐家汇旗舰店 2026-06 起 GMV 断崖
        check:
          type: metric_change         # 支持的类型见下
          metric: gmv                 # 语义层指标名
          filters: {store_id: STORE_S0001}   # 可选,同 query_metric 口径
          base: [2026-05-01, 2026-05-31]
          cmp:  [2026-06-01, 2026-06-30]
          direction: down             # up | down(变化率符号)
          min_rate: 0.30              # |change_rate| 的下/上界(只给其一亦可)
          max_rate: 0.70

check.type 取值:
    - metric_change:比较两段窗口的指标变化率(按上式;derived 指标同样适用)
    - slice_absent:期望某切片在 cmp 窗口完全消失(如 SKU 下架)
        {type: slice_absent, metric: gmv, dimension: product, level: product_id,
         key: SKU_P0001, filters: {store_id: STORE_S0001}, window: [2026-06-01, 2026-06-30]}

每条期望输出:通过 / 不通过 + 实测值(变化率、观测到与否)。
**缺 expectations.yaml 不给「全绿」**:那份文件不存在时回验无法进行,直接抛错并以
退出码 1 结束——「0 条期望、0 条未通过」的报告和「全部通过」在屏幕上长得一样。
**回验独立于造数脚本**:用引擎公开方法实现,不复制造数逻辑——「以为埋了其实没埋」
只有靠独立口径才能发现。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

# 直接 `python scripts/verify_dataset.py` 运行时,项目根不在搜索路径上,先补上
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from attribution.engine import AttributionEngine  # noqa: E402
from harness.datasets import DatasetError, DatasetInfo  # noqa: E402
from scripts.eval_common import (  # noqa: E402
    EXIT_FAILED, EXIT_OK, date_text, dataset_package, fail, read_yaml, required_text,
    semantic_version, window,
)
from scripts.verify_checks import (  # noqa: E402
    CHECK_APPEARED, CHECK_METRIC_CHANGE, CHECK_SLICE_ABSENT, run_expectation,
)

__all__ = ["MissingExpectationsError", "load_expectations", "verify", "main"]

_EXPECTATIONS_NAME = "expectations.yaml"


class MissingExpectationsError(ValueError):
    """数据集没有 expectations.yaml = 没有可回验的黄金断言(不是「0 条期望」)。

    单列一个类型是为了退出码:这是「回验无法进行」的失败(1),不是用法 / 输入错误(2)。
    """

# 每条期望的必填字段 / 各 type 的必填字段(缺了即 schema 非法,抛错不许静默跳过)
_EXPECTATION_FIELDS = ("id", "description", "check")
_REQUIRED_BY_TYPE = {
    CHECK_METRIC_CHANGE: ("metric", "base", "cmp"),
    CHECK_SLICE_ABSENT: ("metric", "dimension", "level", "key", "window"),
    CHECK_APPEARED: ("metric", "base", "window"),
}
_DIRECTIONS = ("up", "down")


def load_expectations(dataset_dir: str) -> dict:
    """读 expectations.yaml -> {dataset_version, expectations: [dict]}。

    文件不存在:**抛错**(MissingExpectationsError)。缺这份文件说明数据集没有黄金断言,
    「0 条期望、0 条通过」的回验报告是 0/0 的「全绿」——那是最危险的一种绿。
    存在但 schema 非法(缺字段 / type 未知):同样抛错,不许静默跳过。
    唯一的「空」是作者显式写出 `expectations: []`(明示没有期望可回验)。
    """
    path = Path(dataset_dir) / _EXPECTATIONS_NAME
    if not path.is_file():
        raise MissingExpectationsError(
            f"数据集没有 {_EXPECTATIONS_NAME},无法回验——不许产出 0/0 全绿:{path}"
            "(没有黄金断言的数据集请先写 expectations.yaml)")
    raw = read_yaml(path)
    if not isinstance(raw, dict):
        raise ValueError(f"expectations 顶层必须是映射(dataset_version / expectations):{path}")
    declared_version = raw.get("dataset_version")
    if declared_version is not None and not isinstance(declared_version, str):
        declared_version = str(declared_version)
    items = raw.get("expectations")
    if not isinstance(items, list):
        # 文件存在却写不出期望列表 = schema 非法(写成 `expectation:` 这种笔误会
        # 把整份回验静默关掉,所以只有「文件不存在」才允许空)
        raise ValueError(f"expectations 必须是列表(实际 {items!r}):{path}")
    expectations = [_build_expectation(item, path, index) for index, item in enumerate(items)]
    ids = [item["id"] for item in expectations]
    if len(set(ids)) != len(ids):
        raise ValueError(f"expectations 存在重复 id(报告会串行):{sorted(_duplicates(ids))}:{path}")
    return {"dataset_version": (declared_version or "").strip(), "expectations": expectations}


def verify(dataset_dir: str) -> dict:
    """逐条回验,返回可 json.dumps 的报告 dict:

      {dataset, dataset_version, total, passed,
       results: [{id, description, passed, detail(实测值或失败原因)}]}
    回验本身用 AttributionEngine 公开方法完成(引擎离线可跑,不需 BI_API_KEY)。
    异常:MissingExpectationsError —— 数据集没有 expectations.yaml(没有可回验的断言)。
    """
    info = dataset_package(dataset_dir)
    spec = load_expectations(str(info.root))
    engine = AttributionEngine(str(info.data_dir), str(info.semantic_path))
    results = [run_expectation(engine, item) for item in spec["expectations"]]
    passed = sum(1 for result in results if result["passed"])
    return {
        "dataset": info.id,
        "dataset_version": _version(info, spec["dataset_version"]),
        "total": len(results),
        "passed": passed,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:--dataset 必填;退出码 0 全部通过 / 1 有未通过或无法回验 / 2 用法错误。"""
    parser = argparse.ArgumentParser(
        description="数据集埋点回验:反向检测每个埋的坑是否可观测、幅度是否匹配声明")
    parser.add_argument("--dataset", required=True, metavar="id|目录",
                        help="数据集 id 或数据集包目录")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:   # 用法错误:argparse 退出码 2;--help 退出码 0 原样透传
        return int(exc.code or EXIT_OK)
    try:
        report = verify(args.dataset)
    except MissingExpectationsError as err:
        return fail(str(err), code=EXIT_FAILED)   # 无法回验 = 失败,不是用法错误
    except (DatasetError, FileNotFoundError, ValueError, OSError) as err:
        return fail(str(err))
    except Exception as err:   # noqa: BLE001  引擎 / 数据层的运行期失败
        return fail(f"回验失败:{err}", code=EXIT_FAILED)
    _print_report(report)
    return EXIT_OK if report["passed"] == report["total"] else EXIT_FAILED


# ---------------------------------------------------------------------------
# 以下为内部实现:schema 校验(坏期望必须显式失败)
# ---------------------------------------------------------------------------
def _build_expectation(item: Any, path: Path, index: int) -> dict:
    """单条期望 -> 归一化 dict{id, description, check};缺字段 / type 未知一律抛错。"""
    where = f"{path} 第 {index + 1} 条"
    if not isinstance(item, dict):
        raise ValueError(f"{where}:期望必须是映射")
    for field in _EXPECTATION_FIELDS:
        if field not in item:
            raise ValueError(f"{where}:缺少必填字段 `{field}`")
    check = item["check"]
    if not isinstance(check, dict):
        raise ValueError(f"{where}:`check` 必须是映射")
    check_type = check.get("type")
    if check_type not in _REQUIRED_BY_TYPE:
        raise ValueError(f"{where}:未知的 check.type {check_type!r}"
                         f"(支持 {'、'.join(sorted(_REQUIRED_BY_TYPE))})")
    for field in _REQUIRED_BY_TYPE[check_type]:
        if check.get(field) in (None, ""):
            raise ValueError(f"{where}:check.type={check_type} 缺少必填字段 `{field}`")
    return {"id": required_text(item["id"], f"{where} 的 id"),
            "description": required_text(item["description"], f"{where} 的 description"),
            "check": _build_check(check, check_type, where)}


def _build_check(check: dict, check_type: str, where: str) -> dict:
    """归一化 check:窗口 / 过滤 / 阈值都先转成好用的形状,回验时不再校验。"""
    common = {"type": check_type, "metric": required_text(check["metric"], f"{where} 的 metric"),
              "filters": _filters(check.get("filters"), where)}
    if check_type == CHECK_METRIC_CHANGE:
        direction = check.get("direction")
        if direction is not None and direction not in _DIRECTIONS:
            raise ValueError(f"{where}:direction 应为 {'/'.join(_DIRECTIONS)} 之一,"
                             f"实际 {direction!r}")
        return {**common, "base": window(check["base"], f"{where} 的 base"),
                "cmp": window(check["cmp"], f"{where} 的 cmp"),
                "direction": direction,
                "min_rate": _rate(check.get("min_rate"), f"{where} 的 min_rate"),
                "max_rate": _rate(check.get("max_rate"), f"{where} 的 max_rate")}
    if check_type == CHECK_APPEARED:
        return {**common, "base": window(check["base"], f"{where} 的 base"),
                "window": window(check["window"], f"{where} 的 window")}
    return {**common, "dimension": required_text(check["dimension"], f"{where} 的 dimension"),
            "level": required_text(check["level"], f"{where} 的 level"),
            "key": required_text(check["key"], f"{where} 的 key"),
            "window": window(check["window"], f"{where} 的 window")}


def _filters(value: Any, where: str) -> dict:
    """可选过滤条件:省略 = {};给了就要求字段名非空、取值是标量(日期归一成文本)。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{where} 的 filters 应为映射(实际 {value!r})")
    filters = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{where} 的 filters 字段名应为非空文本:{value!r}")
        if isinstance(item, (dict, list, tuple)) or item is None:
            raise ValueError(f"{where} 的 filters[{key!r}] 应为标量(实际 {item!r})")
        filters[key] = (date_text(item, f"{where} 的 filters[{key!r}]")
                        if isinstance(item, (date, datetime)) else item)
    return filters


def _rate(value: Any, where: str) -> float | None:
    """可选变化率阈值:非负数(比的是 |change_rate|)。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{where} 应为非负数(实际 {value!r})")
    return float(value)


def _duplicates(values: list[str]) -> set[str]:
    """列表里出现多于一次的元素(用于重复 id 的报错信息)。"""
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in values:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return dupes


# ---------------------------------------------------------------------------
# 以下为内部实现:版本绑定与输出
# ---------------------------------------------------------------------------
def _version(info: DatasetInfo, declared: str) -> str:
    """回验结果绑定的数据集版本:以语义层的 dataset_version 为准(docstring 的「对齐」)。

    语义层读不到时退回 expectations.yaml 的声明,再退回清单 version;
    两者都在且不一致时打一行 stderr 警告(版本对不上说明期望可能写在别的数据上)。
    """
    current = semantic_version(info)
    if current and declared and current != declared:
        print(f"警告:语义层 dataset_version={current} 与 expectations.yaml "
              f"声明的 {declared} 不一致(回验结论按语义层版本记录)", file=sys.stderr)
    return current or declared or info.version


def _print_report(report: dict) -> None:
    """人读的概要:逐条期望的通过情况 + 实测值。"""
    print(f"数据集 {report['dataset']}(v{report['dataset_version']}) 埋点回验")
    if not report["total"]:
        # 只剩一种到达方式:作者显式写了 `expectations: []`(文件缺失已在 load 阶段抛错)
        print("  expectations 列表为空:作者显式声明没有可回验的期望(0/0,不代表通过)")
        return
    for result in report["results"]:
        print(f"  [{'PASS' if result['passed'] else 'FAIL'}] {result['id']}:"
              f"{result['description']}")
        print(f"        {result['detail']}")
    print(f"总计 {report['total']} 条,通过 {report['passed']},"
          f"未通过 {report['total'] - report['passed']}")


if __name__ == "__main__":
    sys.exit(main())
