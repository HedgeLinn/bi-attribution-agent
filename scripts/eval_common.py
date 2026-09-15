"""评估与回验两个脚本共用的基础设施(§5.7 case / §5.10 可回验)。

放在 `evaluate_agent.py` / `verify_dataset.py` 之外的唯一原因:**单文件行数约束(≤300)**。
分层边界不变——本模块只做通用动作(读 YAML、归一字段、解析数据集包、汇总报告、跑一次
真跑 loop),业务判定留在各自脚本里:五维评分在 `evaluate_agent.py`,两条回验通道在
`verify_checks.py`,mock 剧本在 `eval_mock_scripts.py`。

容错口径(两个调用方都依赖这条区别):
  - **人工写的 YAML**(case / expectations)一律显式抛错,坏输入不许静默跳过;
  - **模型输出与运行期环境**(结论 JSON、事件流、语义层不可用)一律容错,不抛错。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from attribution.semantic import Semantic
from harness.datasets import (
    ENV_DATASET_ID, DatasetError, DatasetInfo, load_dataset, resolve_dataset,
)

# 退出码:模块 docstring 写的「0 正常 / 1 用法错误」在实现里细分为三分——
#   0 报告已产出 / 全部通过;1 运行失败(缺 BI_API_KEY、loop / 引擎报错)或回验有未通过;
#   2 用法或输入错误(参数非法、数据集不存在、schema 非法)
EXIT_OK, EXIT_FAILED, EXIT_INPUT = 0, 1, 2

_MANIFEST_NAME = "dataset.yaml"

# 进程内缓存:数据集包目录 -> {维度名: 层级字段元组}(深度维要拿它比较层级位置)
_LEVEL_CACHE: dict[str, dict[str, tuple[str, ...]]] = {}


# ---------------------------------------------------------------------------
# YAML 与字段归一(人工输入:坏数据一律抛错)
# ---------------------------------------------------------------------------
def read_yaml(path: Path) -> Any:
    """读 YAML;解析失败抛错(带文件路径),与 harness.datasets 同口径。"""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as err:
        raise ValueError(f"不是合法 YAML:{path}:{err}") from err


def required_text(value: Any, where: str) -> str:
    """必填文本字段:缺失 / 空白 / 非文本都算坏输入(`where` 说明是谁的哪个字段)。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} 应为非空文本(实际 {value!r})")
    return value.strip()


def is_number(value: Any) -> bool:
    """数字判定:排除 bool(True 也是 int)与字符串。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def to_number(value: Any) -> float | None:
    """转 float;非数字、None、NaN / inf 一律 None(评分数值维宁可跳过,不许抛错)。"""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not is_number(value):
        return None
    number = float(value)
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def positive(value: Any) -> bool:
    """观测判定:非 None、非 0 的数值才算「还看得见」。"""
    return is_number(value) and value != 0


def date_text(value: Any, where: str) -> str:
    """单个日期 -> 'YYYY-MM-DD';datetime.date/datetime 与文本都接受。

    YAML 会把不加引号的 `2026-05-01` 读成 datetime.date(冻结 schema 的示例正是这么写的),
    这里统一归一成引擎要的文本,两种写法都接受。
    """
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} 的日期应为文本或 date(实际 {value!r})")
    return value.strip()


def window(value: Any, where: str) -> tuple[str, str]:
    """日期窗口:恰两个日期 [起, 止]。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{where} 应为 [起, 止] 两个日期(实际 {value!r})")
    return (date_text(value[0], where), date_text(value[1], where))


# ---------------------------------------------------------------------------
# 数据集包与语义层
# ---------------------------------------------------------------------------
def dataset_package(dataset_dir: str) -> DatasetInfo:
    """`--dataset` 的取值(id 或数据集包目录)-> DatasetInfo(与 CLI / 引擎同一套解析)。"""
    path = Path(dataset_dir)
    if path.is_dir() and (path / _MANIFEST_NAME).is_file():
        return load_dataset(path.name, path.parent)
    return resolve_dataset(dataset_dir)


def semantic_version(info: DatasetInfo) -> str:
    """语义层的 dataset_version;读不到返回 ""(版本只是记录项,读不到不影响判定)。"""
    try:
        return Semantic.load(str(info.semantic_path)).dataset_version
    except Exception:   # noqa: BLE001  容错:地图不可用不该让评估 / 回验崩掉
        return ""


def dataset_version(info: DatasetInfo) -> str:
    """评估结果绑定的数据集版本(§3.6①):清单优先,清单没写则退回语义层。"""
    return info.version or semantic_version(info)


def dataset_override() -> str:
    """`BI_DATASET` 的当前取值(空白 = 未设置)。评估期它由 `evaluate()` 临时写入。"""
    return os.environ.get(ENV_DATASET_ID, "").strip()


def depth_levels() -> dict[str, tuple[str, ...]]:
    """当前数据集的「维度名 -> 层级字段」索引(进程内按数据集包缓存)。

    数据集来源:`BI_DATASET` > 磁盘上唯一数据集。变量取值既可以是**数据集 id**,
    也可以是**数据集包的绝对根目录**——`evaluate()` 传的是后者,所以数据集包放在
    仓库外(`--dataset <目录>`,合法用法)时也解析得到,与 CWD 无关。

    解析不到就抛错,不返回 {}:深度维靠这张地图比较层级,**静默退化会把更深的正确答案
    判成不达标**(假失败且无日志)。地图缺失属环境 / 输入错误,必须当场可见。
    """
    override = dataset_override()
    try:
        info = dataset_package(override) if override else resolve_dataset()
    except Exception as err:   # noqa: BLE001  统一换成本模块的错,附上实际取值
        where = f"{ENV_DATASET_ID}={override!r}" if override else "磁盘上唯一数据集"
        raise DatasetError(
            f"深度维拿不到语义层地图({where}):{err}"
            "(数据集包需在磁盘上可解析;评估期该变量由 evaluate() 写成数据集包根目录)"
        ) from err
    cache_key = str(info.root)
    if cache_key not in _LEVEL_CACHE:
        try:
            _LEVEL_CACHE[cache_key] = dict(Semantic.load(str(info.semantic_path)).all_levels())
        except Exception as err:   # noqa: BLE001  地图读不动 = 无法判深度,不许静默跳过
            raise DatasetError(
                f"深度维读语义层失败:{info.semantic_path}:{err}"
            ) from err
    return _LEVEL_CACHE[cache_key]


# ---------------------------------------------------------------------------
# case schema(§5.7):坏 case 必须显式失败,不许静默跳过
# ---------------------------------------------------------------------------
_CASE_FIELDS = ("id", "tier", "category", "question", "expected")
_EXPECTED_FIELDS = ("root_cause_slice", "mechanism", "required_depth")


def build_case(path: Path) -> dict:
    """单个 case YAML -> Case 的字段 dict;缺字段 / 类型不符 / YAML 非法一律抛错。

    (返回 dict 而不是 Case:Case 定义在冻结的 evaluate_agent.py 里,由它自己组装。)
    """
    raw = read_yaml(path)
    if not isinstance(raw, Mapping):
        raise ValueError(f"case 顶层必须是映射:{path}")
    for field in _CASE_FIELDS:
        if field not in raw:
            raise ValueError(f"case 缺必填字段 `{field}`:{path}")
    expected = raw["expected"]
    if not isinstance(expected, Mapping):
        raise ValueError(f"case 的 `expected` 必须是映射:{path}")
    for field in _EXPECTED_FIELDS:
        if field not in expected:
            raise ValueError(f"case 缺必填字段 `expected.{field}`:{path}")
    return {
        "id": required_text(raw["id"], f"case 字段 `id`:{path}"),
        "tier": required_text(raw["tier"], f"case 字段 `tier`:{path}"),
        "category": required_text(raw["category"], f"case 字段 `category`:{path}"),
        "question": required_text(raw["question"], f"case 字段 `question`:{path}"),
        "root_cause_slice": _slice_or_none(expected["root_cause_slice"], path),
        "mechanism": required_text(expected["mechanism"], f"case 字段 `expected.mechanism`:{path}"),
        "contribution_range": _range_or_none(expected.get("contribution_range"), path),
        "required_depth": required_text(expected["required_depth"],
                                        f"case 字段 `expected.required_depth`:{path}"),
        "must_not_claim": _text_tuple(_pick(expected, raw, "must_not_claim"),
                                      "must_not_claim", path),
        "distractors": _text_tuple(raw.get("distractors"), "distractors", path),
        "raw": dict(raw),
    }


def _pick(expected: Mapping, raw: Mapping, field: str) -> Any:
    """按 §5.7 的 schema 取字段:`expected` 里优先,顶层写法也容忍。

    (must_not_claim 在 §5.7 的示例里位于 `expected` 下;写成顶层曾让这一维静默失效 ——
    评分维度悄悄变成空断言是最危险的一类 bug,所以两种写法都读。)
    """
    return expected[field] if field in expected else raw.get(field)


def _slice_or_none(value: Any, path: Path) -> dict | None:
    """根因切片:null(无法归因类)或含 dimension/level/key 的映射。"""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"case 字段 `expected.root_cause_slice` 应为映射或 null:{path}")
    for field in ("dimension", "level", "key"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"case 字段 `expected.root_cause_slice.{field}` 应为非空文本:{path}")
    return dict(value)


def _range_or_none(value: Any, path: Path) -> tuple[float, float] | None:
    """贡献度区间:[下限, 上限] 两个数字,或 null(不评数值维)。"""
    if value is None:
        return None
    if (not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 2
            or any(not is_number(item) for item in value)):
        raise ValueError(f"case 字段 `expected.contribution_range` 应为两个数字或 null:{path}")
    return (float(value[0]), float(value[1]))


def _text_tuple(value: Any, field: str, path: Path) -> tuple[str, ...]:
    """短语列表字段(must_not_claim / distractors):省略算空,给了就必须是文本列表。"""
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"case 字段 `{field}` 应为文本列表:{path}")
    return tuple(required_text(item, f"case 字段 `{field}`:{path}") for item in value)


# ---------------------------------------------------------------------------
# 模型输出的容错解析(评分侧:任何形状都不许抛错)
# ---------------------------------------------------------------------------
def strip_fences(content: str) -> str:
    """去掉模型可能包在 JSON 外的 ``` 围栏(与 harness.loop 同口径)。"""
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def try_json(text: str) -> dict | None:
    """严格解析一段 JSON;非映射(数组/标量)与语法错误都返回 None。"""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return dict(obj) if isinstance(obj, Mapping) else None


def as_mapping(value: Any) -> Mapping:
    """YAML / 模型输出里的节点统一成 Mapping(不是映射就给空 Mapping,调用方判缺)。"""
    return value if isinstance(value, Mapping) else {}


def blob(value: Any) -> str:
    """把结论/已排除的任意形状拼成一段可做子串匹配的文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Sequence):
        return "\n".join(blob(item) for item in value)
    return str(value)


# ---------------------------------------------------------------------------
# CLI 公共出口
# ---------------------------------------------------------------------------
def fail(message: str, code: int = EXIT_INPUT) -> int:
    """用法 / 输入错误:打到 stderr 并返回退出码(不抛 SystemExit,便于测试)。"""
    print(f"错误:{message}", file=sys.stderr)
    return code
