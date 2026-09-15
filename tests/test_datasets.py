"""数据集包的发现与解析测试(docs/REUSE_DESIGN.md §3.3)。

全部用 tmp_path 现造数据集包,不依赖真实数据——
这样换数据、迁目录都不会让这些用例失效。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from harness.datasets import (
    ENV_DATASET_ID,
    MANIFEST_NAME,
    DatasetError,
    DatasetInfo,
    discover_datasets,
    load_dataset,
    resolve_dataset,
)

# 语义层桩文件:本模块只解析路径,不加载语义层,内容无关紧要
SEMANTIC_STUB = 'schema_version: "2.0"\n'

# 合法清单模板(字段与 §3.3 一致)
MANIFEST_TEMPLATE = """\
id: {dataset_id}
title: {title}
description: 测试用数据集
version: "1.0.0"
industry: 零售
semantic: semantic.yaml
data_dir: data
case_count: {case_count}
"""


def make_dataset(
    datasets_dir: Path,
    dataset_id: str,
    *,
    title: str | None = None,
    manifest: str | None = None,
) -> Path:
    """造一个数据集包(清单 + 语义层 + data/ 占位),返回包根目录。

    `manifest` 传入时按原文写入,用于构造非法清单;`title` 省略则用 id 兜底。
    """
    root = datasets_dir / dataset_id
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "semantic.yaml").write_text(SEMANTIC_STUB, encoding="utf-8")
    text = manifest if manifest is not None else MANIFEST_TEMPLATE.format(
        dataset_id=dataset_id, title=title or dataset_id, case_count=6,
    )
    (root / MANIFEST_NAME).write_text(textwrap.dedent(text), encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _clear_dataset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认清掉 BI_DATASET:开发机上的环境变量不该影响用例结果。"""
    monkeypatch.delenv(ENV_DATASET_ID, raising=False)


# ---------------------------------------------------------------------------
# discover_datasets:发现与排序
# ---------------------------------------------------------------------------
def test_discover_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    """目录不存在不是错误:允许「还没有任何数据集」的状态。"""
    assert discover_datasets(tmp_path / "没有这个目录") == []


def test_discover_skips_dir_without_manifest(tmp_path: Path) -> None:
    """没有 dataset.yaml 的目录不是数据集包,跳过而不是报错。"""
    (tmp_path / "not-a-dataset").mkdir()
    (tmp_path / "not-a-dataset" / "readme.txt").write_text("x", encoding="utf-8")
    assert discover_datasets(tmp_path) == []


def test_discover_skips_plain_files(tmp_path: Path) -> None:
    """数据集目录下的普通文件不该被当成数据集。"""
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    assert discover_datasets(tmp_path) == []


def test_discover_sorts_by_id(tmp_path: Path) -> None:
    """发现结果按 id 排序,与目录创建顺序无关。"""
    for dataset_id in ("zeta", "alpha", "mid"):
        make_dataset(tmp_path, dataset_id)
    assert [info.id for info in discover_datasets(tmp_path)] == ["alpha", "mid", "zeta"]


def test_discover_parses_manifest_fields(tmp_path: Path) -> None:
    """清单字段逐项落到 DatasetInfo:元信息 + 相对路径解析成包内路径。"""
    root = make_dataset(tmp_path, "alpha", title="零售电商归因")
    info = discover_datasets(tmp_path)[0]
    assert (info.id, info.title) == ("alpha", "零售电商归因")
    assert info.description == "测试用数据集"
    assert info.version == "1.0.0"
    assert info.industry == "零售"
    assert info.case_count == 6
    assert info.root == root
    assert info.semantic_path == root / "semantic.yaml"
    assert info.data_dir == root / "data"


def test_discover_returns_pathlib_paths(tmp_path: Path) -> None:
    """路径一律是 pathlib.Path(不返回字符串)。"""
    make_dataset(tmp_path, "alpha")
    info = discover_datasets(tmp_path)[0]
    assert isinstance(info, DatasetInfo)
    for value in (info.root, info.semantic_path, info.data_dir):
        assert isinstance(value, Path)


def test_discover_does_not_require_semantic_or_data(tmp_path: Path) -> None:
    """发现阶段只认清单:语义层/数据目录缺失留给引擎在打开时报错。"""
    root = tmp_path / "alpha"
    root.mkdir()
    (root / MANIFEST_NAME).write_text("id: alpha\ntitle: 只有清单\n", encoding="utf-8")
    assert [info.id for info in discover_datasets(tmp_path)] == ["alpha"]


# ---------------------------------------------------------------------------
# 非法清单:一律抛 DatasetError,不静默跳过
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "manifest",
    [
        pytest.param("id: [未闭合\n", id="yaml-语法错误"),
        pytest.param("- 这不是映射\n", id="顶层不是-mapping"),
        pytest.param("title: 缺 id\n", id="缺必填字段-id"),
        pytest.param("id: alpha\n", id="缺必填字段-title"),
        pytest.param("id: beta\ntitle: 与目录名不符\n", id="id-与目录名不符"),
        pytest.param("id: alpha\ntitle: 越界\ndata_dir: ../outside\n", id="data_dir-越界"),
        pytest.param("id: alpha\ntitle: 绝对路径\nsemantic: C:/tmp/semantic.yaml\n", id="semantic-绝对路径"),
        pytest.param("id: alpha\ntitle: 类型错\ncase_count: 六个\n", id="case_count-非整数"),
    ],
)
def test_discover_raises_on_invalid_manifest(tmp_path: Path, manifest: str) -> None:
    """清单存在但非法 = 配置错误,必须被看见(同时覆盖 load_dataset 的同一校验路径)。"""
    make_dataset(tmp_path, "alpha", manifest=manifest)
    with pytest.raises(DatasetError):
        discover_datasets(tmp_path)


# ---------------------------------------------------------------------------
# load_dataset:按 id 取单个数据集
# ---------------------------------------------------------------------------
def test_load_dataset_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(DatasetError):
        load_dataset("查无此集", tmp_path)


def test_load_dataset_missing_manifest_raises(tmp_path: Path) -> None:
    """目录在、清单不在:同样不可用。"""
    (tmp_path / "alpha").mkdir()
    with pytest.raises(DatasetError):
        load_dataset("alpha", tmp_path)


def test_load_dataset_returns_matching_id(tmp_path: Path) -> None:
    make_dataset(tmp_path, "alpha", title="甲")
    make_dataset(tmp_path, "beta", title="乙")
    assert load_dataset("beta", tmp_path).title == "乙"


def test_load_dataset_defaults_optional_fields(tmp_path: Path) -> None:
    """可选字段缺省:语义层默认 semantic.yaml,数据目录默认 data,元信息留空。"""
    root = make_dataset(tmp_path, "alpha", manifest="id: alpha\ntitle: 最小清单\n")
    info = load_dataset("alpha", tmp_path)
    assert info.semantic_path == root / "semantic.yaml"
    assert info.data_dir == root / "data"
    assert (info.description, info.version, info.industry, info.case_count) == ("", "", None, None)


# ---------------------------------------------------------------------------
# resolve_dataset:四级优先级
# ---------------------------------------------------------------------------
def test_resolve_explicit_paths_win_over_dataset_id(tmp_path: Path) -> None:
    """① > ②:显式 data_dir + semantic_path 时不看 dataset_id,也不读清单。"""
    make_dataset(tmp_path, "alpha")
    outside = tmp_path / "outside"
    outside.mkdir()
    semantic = outside / "my.yaml"
    semantic.write_text(SEMANTIC_STUB, encoding="utf-8")

    info = resolve_dataset("alpha", data_dir=outside, semantic_path=semantic,
                           datasets_dir=tmp_path)
    assert info.id != "alpha", "显式路径必须压过 dataset_id"
    assert info.data_dir == outside
    assert info.semantic_path == semantic


def test_resolve_explicit_paths_win_over_single_dataset(tmp_path: Path) -> None:
    """① > ③:即使磁盘上只有一个数据集,显式路径也优先。"""
    make_dataset(tmp_path, "alpha")
    outside = tmp_path / "mydata"
    outside.mkdir()
    semantic = outside / "semantic.yaml"
    semantic.write_text(SEMANTIC_STUB, encoding="utf-8")

    info = resolve_dataset(data_dir=outside, semantic_path=semantic, datasets_dir=tmp_path)
    assert info.data_dir == outside


def test_resolve_explicit_paths_must_be_paired(tmp_path: Path) -> None:
    """只给一条路径属于意图不明,直接报错而不是悄悄降级到别的数据集。"""
    outside = tmp_path / "mydata"
    outside.mkdir()
    with pytest.raises(DatasetError):
        resolve_dataset(data_dir=outside, datasets_dir=tmp_path)
    with pytest.raises(DatasetError):
        resolve_dataset(semantic_path=outside / "semantic.yaml", datasets_dir=tmp_path)


def test_resolve_explicit_paths_must_exist(tmp_path: Path) -> None:
    """显式路径是用户输入:不存在就当场报错,不留到引擎里报。"""
    exists = tmp_path / "mydata"
    exists.mkdir()
    semantic = exists / "semantic.yaml"
    semantic.write_text(SEMANTIC_STUB, encoding="utf-8")

    with pytest.raises(DatasetError):
        resolve_dataset(data_dir=exists / "无此目录", semantic_path=semantic)
    with pytest.raises(DatasetError):
        resolve_dataset(data_dir=exists, semantic_path=exists / "无此文件.yaml")


def test_resolve_by_dataset_id(tmp_path: Path) -> None:
    """② 显式 id:多数据集时按 id 选中。"""
    make_dataset(tmp_path, "alpha", title="甲")
    make_dataset(tmp_path, "beta", title="乙")
    assert resolve_dataset("beta", datasets_dir=tmp_path).title == "乙"


def test_resolve_by_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """② 环境变量 BI_DATASET 等价于 --dataset。"""
    make_dataset(tmp_path, "alpha", title="甲")
    make_dataset(tmp_path, "beta", title="乙")
    monkeypatch.setenv(ENV_DATASET_ID, "beta")
    assert resolve_dataset(datasets_dir=tmp_path).id == "beta"


def test_resolve_explicit_id_beats_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """② 内部:显式参数优先于环境变量。"""
    make_dataset(tmp_path, "alpha")
    make_dataset(tmp_path, "beta")
    monkeypatch.setenv(ENV_DATASET_ID, "beta")
    assert resolve_dataset("alpha", datasets_dir=tmp_path).id == "alpha"


def test_resolve_blank_env_var_treated_as_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空字符串等于没设:继续走 ③ 自动选中,而不是报「数据集 '' 不存在」。"""
    make_dataset(tmp_path, "alpha")
    monkeypatch.setenv(ENV_DATASET_ID, "   ")
    assert resolve_dataset(datasets_dir=tmp_path).id == "alpha"


def test_resolve_auto_selects_single_dataset(tmp_path: Path) -> None:
    """③ 零配置:磁盘上恰好一个数据集时自动用它。"""
    root = make_dataset(tmp_path, "only-one", title="唯一数据集")
    info = resolve_dataset(datasets_dir=tmp_path)
    assert info.id == "only-one"
    assert info.data_dir == root / "data"


def test_resolve_multiple_datasets_requires_explicit_choice(tmp_path: Path) -> None:
    """④ 多数据集且未指定:报错,并把可选 id 全列出来。"""
    make_dataset(tmp_path, "alpha")
    make_dataset(tmp_path, "beta")
    with pytest.raises(DatasetError) as excinfo:
        resolve_dataset(datasets_dir=tmp_path)
    message = str(excinfo.value)
    assert "alpha" in message and "beta" in message


def test_resolve_no_dataset_at_all_raises(tmp_path: Path) -> None:
    """④ 一个数据集都没有:报错并给出补救方式。"""
    with pytest.raises(DatasetError) as excinfo:
        resolve_dataset(datasets_dir=tmp_path)
    assert "数据集" in str(excinfo.value)


def test_resolve_unknown_id_raises(tmp_path: Path) -> None:
    """② 指定了不存在的 id:报错,不退回自动选中。"""
    make_dataset(tmp_path, "alpha")
    with pytest.raises(DatasetError):
        resolve_dataset("查无此集", datasets_dir=tmp_path)
