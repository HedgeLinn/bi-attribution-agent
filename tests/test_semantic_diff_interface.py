# -*- coding: utf-8 -*-
"""semantic_diff 的接口契约:输入形态、异常、以及 check_semantic.py 的退出码。

§3.6③ 要的是「机械检测,不靠人记」——那就必须有一条能被 CI 读到的信号(退出码),
以及一个不会把「比不了」说成「无变更」的输入契约。两件事都在这里钉死。

级别判定本身见 tests/test_semantic_diff.py;夹具见 tests/semantic_diff_fixtures.py。
"""

import pytest
import yaml

from attribution.semantic import Semantic, SemanticError
from attribution.semantic_diff import diff_layers
from scripts.check_semantic import EXIT_INPUT, EXIT_OK, main
from tests.semantic_diff_fixtures import base_layer, diff_paths, write_layer


# ----------------------------------------------------------------------
# 输入形态:文件路径 / Semantic 实例 / 不支持的来源
# ----------------------------------------------------------------------
def test_semantic_instance_input_agrees_with_path_input(tmp_path):
    """文件路径与 Semantic 实例两种输入形态给出同一结果(实体部分走同一批字段)。"""
    new_layer = base_layer()
    new_layer["metrics"]["basket"]["label"] = "件单价"
    old_path = write_layer(tmp_path, base_layer(), "old.yaml")
    new_path = write_layer(tmp_path, new_layer, "new.yaml")

    from_path = diff_layers(old_path, new_path)
    assert from_path, "夹具本身要产生变更,否则这条断言会退化成假绿"
    assert from_path == diff_layers(Semantic.load(old_path), Semantic.load(new_path))


def test_semantic_instance_cannot_see_untyped_sections(tmp_path):
    """Semantic 实例不保留 YAML 原文 -> 三段知识块不可比,但不能误报成「删除」。"""
    new_layer = base_layer()
    del new_layer["decompositions"]
    del new_layer["caveats"]
    old_path = write_layer(tmp_path, base_layer(), "old.yaml")
    new_path = write_layer(tmp_path, new_layer, "new.yaml")

    assert diff_paths(tmp_path, base_layer(), new_layer)        # 路径输入看得见删除
    assert diff_layers(Semantic.load(old_path), Semantic.load(new_path)) == []  # 实例看不见


def test_unsupported_source_type_raises(tmp_path):
    """来源类型不支持 -> TypeError(不静默返回空列表,那会被读成「无变更」)。"""
    with pytest.raises(TypeError):
        diff_layers(123, write_layer(tmp_path, base_layer(), "new.yaml"))


def test_missing_file_raises_semantic_error(tmp_path):
    """文件读不到 -> SemanticError(库层不吞异常,退出码由 CLI 决定)。"""
    with pytest.raises(SemanticError):
        diff_layers(str(tmp_path / "nope.yaml"), write_layer(tmp_path, base_layer(), "new.yaml"))


# ----------------------------------------------------------------------
# CLI:退出码必须能被 CI 读到
# ----------------------------------------------------------------------
def test_cli_identical_layers_exit_zero(tmp_path, capsys):
    """无变更:退出码 0,并明确打印「无变更」。"""
    path = write_layer(tmp_path, base_layer(), "same.yaml")
    assert main(["--old", path, "--new", path]) == EXIT_OK
    assert "无变更" in capsys.readouterr().out


def test_cli_breaking_change_exits_nonzero_with_must_do_hint(tmp_path, capsys):
    """有破坏性变更:退出码非 0,逐条列出,并给出 bump 版本 + 重算 case 的提示。"""
    new_layer = base_layer()
    new_layer["metrics"]["revenue"]["expression"] = "SUM(net_amount)"
    code = main(["--old", write_layer(tmp_path, base_layer(), "old.yaml"),
                 "--new", write_layer(tmp_path, new_layer, "new.yaml")])
    out = capsys.readouterr().out
    assert code != 0
    assert "破坏性" in out and "metrics.revenue.expression" in out
    assert "bump dataset_version" in out and "重算受影响 case" in out


def test_cli_incremental_change_exits_zero(tmp_path, capsys):
    """只有增量变更:退出码 0,但仍把变更逐条列出来。"""
    new_layer = base_layer()
    new_layer["metrics"]["basket"]["label"] = "件单价"
    code = main(["--old", write_layer(tmp_path, base_layer(), "old.yaml"),
                 "--new", write_layer(tmp_path, new_layer, "new.yaml")])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "metrics.basket.label" in out and "仅增量变更" in out


def test_cli_broken_yaml_exits_input_error(tmp_path, capsys):
    """YAML 非法 / 文件读不到 -> 退出码 2(不能与「破坏性变更」的 1 混淆)。"""
    broken = tmp_path / "broken.yaml"
    broken.write_text("metrics: [这不是映射]", encoding="utf-8")
    code = main(["--old", str(broken), "--new", write_layer(tmp_path, base_layer(), "new.yaml")])
    assert code == EXIT_INPUT
    assert "错误" in capsys.readouterr().err


def test_cli_missing_arguments_exit_input_error(capsys):
    """参数不完整 / 两种模式混用 -> 退出码 2。"""
    assert main(["--old", "some.yaml"]) == EXIT_INPUT
    assert main(["--dataset", "some-id", "--old", "some.yaml"]) == EXIT_INPUT
    assert "错误" in capsys.readouterr().err


def test_cli_unknown_dataset_exits_input_error(capsys):
    """数据集不存在 -> 退出码 2,并把无法解析的原因打出来(不静默换一个数据集跑)。"""
    assert main(["--dataset", "no-such-dataset"]) == EXIT_INPUT
    assert "无法解析" in capsys.readouterr().err


# ----------------------------------------------------------------------
# 破坏性变更的影响分析:列出具体 case
# ----------------------------------------------------------------------
def _make_dataset_package(tmp_path) -> tuple[str, str]:
    """造一个 datasets/<id>/ 布局的数据集包,返回 (新语义层路径, 数据集 id)。"""
    root = tmp_path / "diff-fixture"
    (root / "cases").mkdir(parents=True)
    (root / "dataset.yaml").write_text(
        "id: diff-fixture\ntitle: 假数据集\nversion: \"1.0.0\"\n", encoding="utf-8")
    (root / "cases" / "c1.yaml").write_text("id: c1\n", encoding="utf-8")
    (root / "cases" / "c2.yaml").write_text("id: c2\n", encoding="utf-8")
    new_layer = base_layer()
    new_layer["metrics"]["revenue"]["expression"] = "SUM(net_amount)"   # 破坏性
    new_path = write_layer(root, new_layer, "semantic.yaml")
    return new_path, "diff-fixture"


def test_cli_breaking_change_lists_affected_cases(tmp_path, capsys):
    """破坏性变更时:影响分析要落到具体 case,而不是泛泛一句「重算受影响 case」。"""
    new_path, _ = _make_dataset_package(tmp_path)
    old_path = write_layer(tmp_path, base_layer(), "old.yaml")
    code = main(["--old", old_path, "--new", new_path])
    out = capsys.readouterr().out
    assert code != 0
    assert "受影响 case(2 个" in out and "c1、c2" in out


def test_cli_breaking_change_outside_package_degrades_to_hint(tmp_path, capsys):
    """语义层不在数据集包布局里 -> 影响分析降级为提示,但不改变破坏性的退出码。"""
    new_layer = base_layer()
    new_layer["metrics"]["revenue"]["expression"] = "SUM(net_amount)"
    new_path = write_layer(tmp_path, new_layer, "new.yaml")
    old_path = write_layer(tmp_path, base_layer(), "old.yaml")
    code = main(["--old", old_path, "--new", new_path])
    out = capsys.readouterr().out
    assert code != 0
    assert "未能定位数据集包" in out
    assert "个,须重算贡献区间" not in out      # 影响分析行没出现(只有 _MUST_DO 的泛泛提示)
