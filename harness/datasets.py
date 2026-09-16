"""数据集包的发现与解析(docs/REUSE_DESIGN.md §3.3)。

一个数据集 = `datasets/<id>/` 目录:

    datasets/ecommerce-demo/
    ├── dataset.yaml       # 元信息(本模块读它)
    ├── semantic.yaml      # 语义层(那张"地图")
    ├── expectations.yaml  # 数据集专属黄金断言(M4)
    ├── annotations.jsonl  # 归因结论沉淀(M6)
    └── data/*.parquet

设计约束:
    - 本模块**只做发现与解析**,不加载语义层、不碰 DuckDB、不碰 LLM
    - 不认识任何具体数据集:目录里有什么就返回什么
    - 路径一律返回 `pathlib.Path`,由调用方决定怎么用
"""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

__all__ = ["DatasetError", "DatasetInfo", "discover_datasets", "load_dataset", "resolve_dataset"]

# 数据集包的固定文件名(§3.3)。改这里等于改约定,须同步文档。
MANIFEST_NAME = "dataset.yaml"
SEMANTIC_NAME = "semantic.yaml"
DEFAULT_DATA_DIRNAME = "data"

# 默认数据集目录(相对项目根)
DEFAULT_DATASETS_DIRNAME = "datasets"

# 清单必填字段:id 用于定位数据集,title 用于前端下拉展示;其余字段都有默认值
REQUIRED_MANIFEST_FIELDS = ("id", "title")

# 不写 --dataset 时的数据集 id 来源(§3.3 的「等价环境变量」)
ENV_DATASET_ID = "BI_DATASET"

# 显式 data_dir + semantic_path(自带数据、没有数据集包)时的占位身份
EXPLICIT_DATASET_ID = "custom"
EXPLICIT_DATASET_TITLE = "自定义数据集(显式 data-dir + semantic)"


class DatasetError(Exception):
    """数据集包缺失、清单非法或无法解析。"""


@dataclass(frozen=True)
class DatasetInfo:
    """一个数据集包的元信息 + 解析后的路径。"""

    id: str
    title: str
    root: Path            # datasets/<id>/
    semantic_path: Path   # 语义层入口
    data_dir: Path        # parquet 目录
    description: str = ""
    version: str = ""
    industry: str | None = None
    case_count: int | None = None
    # 这份地图是否**从未经过人工确认**(导入时跳过了确认向导,见 harness/import_confirm.py)。
    # 缺省 False:4 套手写数据集没有这个字段,不该被判成未确认。
    unconfirmed: bool = False


def discover_datasets(datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME) -> list[DatasetInfo]:
    """扫描 `datasets/*/`,返回全部数据集,**按 id 排序**。

    没有 `dataset.yaml` 的目录会被跳过(不是数据集包)。
    目录不存在 -> 返回空列表(不抛错:允许"还没有任何数据集"的状态)。
    清单存在但非法 -> 抛 DatasetError(不静默跳过:那是配置错误,该被看见)。
    """
    base = Path(datasets_dir)
    if not base.is_dir():
        return []
    found = [
        load_dataset(child.name, base)
        for child in sorted(base.iterdir())
        if child.is_dir() and (child / MANIFEST_NAME).is_file()
    ]
    return sorted(found, key=lambda info: info.id)


def load_dataset(
    dataset_id: str,
    datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME,
) -> DatasetInfo:
    """按 id 取一个数据集。

    异常:
        DatasetError: 目录不存在、清单缺失或非法、id 与目录名不符。
    """
    root = Path(datasets_dir) / dataset_id
    if not root.is_dir():
        raise DatasetError(f"数据集目录不存在:{root}")
    manifest = root / MANIFEST_NAME
    if not manifest.is_file():
        raise DatasetError(f"数据集 {dataset_id!r} 缺少清单 {MANIFEST_NAME}:{manifest}")
    raw = _read_manifest(manifest)
    declared_id = _required_text(raw, "id", manifest)
    if declared_id != dataset_id:
        raise DatasetError(
            f"清单 id {declared_id!r} 与目录名 {dataset_id!r} 不符:{manifest}"
            "(id 必须等于目录名,否则 --dataset 指到的与实际跑的数据会对不上)"
        )
    return _build_info(root, raw, manifest, declared_id)


def resolve_dataset(
    dataset_id: str | None = None,
    data_dir: str | Path | None = None,
    semantic_path: str | Path | None = None,
    datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME,
) -> DatasetInfo:
    """统一解析入口,优先级从高到低:

        ① 显式传 `data_dir` + `semantic_path`(允许用户自带数据,不需要 dataset.yaml)
        ② 显式传 `dataset_id`,或环境变量 `BI_DATASET`
        ③ 磁盘上恰好只有一个数据集 -> 用它(零配置即可跑)
        ④ 都没有 / 有多个但没指定 -> 抛 DatasetError,并把可选 id 列进错误信息

    规则 ③ 是刻意的:只有一个数据集时不该逼用户写 `--dataset`;
    多于一个时必须显式选择,避免"以为是 A 结果跑了 B"。
    """
    if data_dir is not None or semantic_path is not None:
        # ① 自带数据:两条路径必须成对给出,缺一条说明用户意图不明
        return _explicit_info(data_dir, semantic_path)

    requested = _requested_id(dataset_id)
    if requested is not None:
        # ② 显式 id(或环境变量)
        return load_dataset(requested, datasets_dir)

    found = discover_datasets(datasets_dir)
    if len(found) == 1:
        # ③ 零配置:磁盘上恰好只有一个数据集
        return found[0]

    # ④ 无法唯一确定
    raise DatasetError(_no_dataset_message(found, datasets_dir))


# ---------------------------------------------------------------------------
# 以下为内部实现:清单读取与校验、路径解析
# ---------------------------------------------------------------------------
def _explicit_info(data_dir: str | Path | None, semantic_path: str | Path | None) -> DatasetInfo:
    """由显式路径构造 DatasetInfo(规则 ①,不读 dataset.yaml)。

    用户自带数据时不存在数据集包,`root` 取语义层所在目录——
    数据集包里清单与语义层同级,这个位置对后续产物(expectations/annotations)最接近。

    异常:
        DatasetError: 只给了其中一条路径,或路径不存在。
    """
    if data_dir is None or semantic_path is None:
        raise DatasetError(
            "data_dir 与 semantic_path 必须同时给出(自带数据需要自己指定语义层):"
            f"当前 data_dir={data_dir!r}, semantic_path={semantic_path!r}"
        )
    data_path, semantic = Path(data_dir), Path(semantic_path)
    if not semantic.is_file():
        raise DatasetError(f"语义层文件不存在:{semantic}")
    if not data_path.is_dir():
        raise DatasetError(f"数据目录不存在:{data_path}")
    return DatasetInfo(
        id=EXPLICIT_DATASET_ID,
        title=EXPLICIT_DATASET_TITLE,
        root=semantic.parent,
        semantic_path=semantic,
        data_dir=data_path,
        description="由 data_dir + semantic_path 显式指定,未读取 dataset.yaml",
    )


def _requested_id(dataset_id: str | None) -> str | None:
    """本次调用要求的数据集 id:显式参数优先于环境变量,空白视为未指定。"""
    for candidate in (dataset_id, os.environ.get(ENV_DATASET_ID)):
        if candidate and candidate.strip():
            return candidate.strip()
    return None


def _no_dataset_message(found: list[DatasetInfo], datasets_dir: str | Path) -> str:
    """规则 ④ 的错误信息:必须把可选 id 列出来,否则用户不知道该填什么。"""
    if not found:
        return (
            f"未找到任何数据集:{datasets_dir} 下没有带 {MANIFEST_NAME} 的目录。"
            "请新建数据集包,或用 --data-dir + --semantic 显式指定数据。"
        )
    ids = "、".join(info.id for info in found)
    return (
        f"发现 {len(found)} 个数据集,必须显式选择:{ids}。"
        f"用 --dataset <id> 或环境变量 {ENV_DATASET_ID}=<id> 指定。"
    )


def _read_manifest(path: Path) -> dict:
    """读清单 YAML;读不动、解析失败或顶层不是映射都抛 DatasetError。"""
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise DatasetError(f"清单不是合法 YAML:{path}:{exc}") from exc
    except OSError as exc:
        raise DatasetError(f"清单读取失败:{path}:{exc}") from exc
    if not isinstance(raw, dict):
        raise DatasetError(f"清单顶层必须是映射(至少含 {'/'.join(REQUIRED_MANIFEST_FIELDS)}):{path}")
    return raw


def _build_info(root: Path, raw: dict, manifest: Path, dataset_id: str) -> DatasetInfo:
    """清单 -> DatasetInfo:清单里的相对名字解析成 root 下的路径,可选字段给默认值。

    只读**已知字段**,未知字段静默忽略 —— 所以给清单加字段(dataset.yaml 的
    `unconfirmed`)对既有数据集与本模块都是向后兼容的。
    """
    semantic_name = _optional_text(raw, "semantic", manifest) or SEMANTIC_NAME
    data_dirname = _optional_text(raw, "data_dir", manifest) or DEFAULT_DATA_DIRNAME
    return DatasetInfo(
        id=dataset_id,
        title=_required_text(raw, "title", manifest),
        root=root,
        semantic_path=_child_path(root, semantic_name, "semantic", manifest),
        data_dir=_child_path(root, data_dirname, "data_dir", manifest),
        description=_optional_text(raw, "description", manifest) or "",
        version=_optional_text(raw, "version", manifest) or "",
        industry=_optional_text(raw, "industry", manifest),
        case_count=_optional_int(raw, "case_count", manifest),
        unconfirmed=_optional_bool(raw, "unconfirmed", manifest),
    )


def _coerce_text(value: object, field: str, manifest: Path) -> str | None:
    """YAML 标量 -> 文本;null 返回 None。数字也接受(如 `version: 1.0` 会被 YAML 读成 float)。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise DatasetError(f"清单字段 `{field}` 应为文本,实际是 {type(value).__name__}:{manifest}")


def _required_text(raw: dict, field: str, manifest: Path) -> str:
    """必填文本字段:缺失或空白即清单非法。"""
    text = _coerce_text(raw.get(field), field, manifest)
    if not text:
        raise DatasetError(f"清单缺少必填字段 `{field}`(非空文本):{manifest}")
    return text


def _optional_text(raw: dict, field: str, manifest: Path) -> str | None:
    """可选文本字段(description/version/industry/semantic/data_dir)。"""
    return _coerce_text(raw.get(field), field, manifest)


def _optional_int(raw: dict, field: str, manifest: Path) -> int | None:
    """可选整数字段(case_count):接受 int 与数字字符串,其余视为清单非法。"""
    value = raw.get(field)
    if value is None:
        return None
    if isinstance(value, bool):
        raise DatasetError(f"清单字段 `{field}` 应为整数:{manifest}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("+").isdigit():
        return int(value.strip())
    raise DatasetError(f"清单字段 `{field}` 应为整数,实际是 {value!r}:{manifest}")


def _optional_bool(raw: dict, field: str, manifest: Path) -> bool:
    """可选布尔字段(unconfirmed):缺省 False,只认 YAML 布尔与 true/false 文本。

    比 _optional_int 严格:这里没有「数字字符串」那种合理写法,乱写(如 unconfirmed: 1)
    就该被看见,而不是静默当成 True / False 之一。
    """
    value = raw.get(field)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise DatasetError(f"清单字段 `{field}` 应为布尔值,实际是 {value!r}:{manifest}")


def _child_path(root: Path, name: str, field: str, manifest: Path) -> Path:
    """清单里的相对名字 -> root 下的路径;绝对路径与越界路径一律拒绝。"""
    if not name.strip():
        raise DatasetError(f"清单字段 `{field}` 不能为空:{manifest}")
    relative = Path(name.strip())
    if relative.is_absolute() or ".." in relative.parts:
        raise DatasetError(
            f"清单字段 `{field}` 必须是数据集目录内的相对路径(实际 {name!r}):{manifest}"
        )
    return root / relative
