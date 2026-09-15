"""机械防回退:引擎代码里不得出现任何具体数据集的词汇。

对应 docs/REUSE_DESIGN.md §3.6⑤——把「引擎不认识具体数据集」从设计原则
变成一条会失败的测试,防止 _NAME_COL 那类硬编码悄悄长回来。

**扫描策略:默认包含 + 例外显式排除。**
attribution/ 下的模块默认**全部**纳入扫描,只有例外清单里的数据集专属脚本才跳过。
方向不能反过来:写死「要扫哪几个文件」时,新拆出来的模块(如 sql_source.py,
表名 / 别名 / JOIN 的唯一产生地)会整块逃过检查,而没人会记得回来改清单;
默认全扫则新增模块自动被覆盖,漏检只可能来自例外清单,而清单本身也被测试盯着。

**为什么必须解析 AST,而不是 `name in 源码文本`**:
engine.py / expression.py 的 docstring 里有大量举例(`'SUM(amount)'`、
`'gmv / NULLIF(orders_count, 0)'`),docstring 是文档不是实现。
直接做子串匹配会立刻假阳性,所以只检查两类「实现痕迹」:
    - 标识符:ast.Name / ast.Attribute / 函数与类名 / 形参与关键字参数名
    - 字符串字面量:ast.Constant(str),并剔除 docstring 对应的节点;
      ASCII 词按词切分,含 CJK 的字面量整串入集(抓中文**实体值**,如「上海徐家汇旗舰店」)
再断言两者与词汇表**无交集**;命中即失败,失败信息点名文件与词汇。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

try:  # tests/ 是包(存在 __init__.py)
    from tests.conftest import collect_vocabulary_from_all_datasets, discover_semantic_files
except ImportError:  # tests/ 不是包:由 pythonpath=. 兜底
    from conftest import (  # type: ignore[no-redef]
        collect_vocabulary_from_all_datasets,
        discover_semantic_files,
    )

# 项目根目录:tests/test_*.py -> 上一级
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 引擎包:扫描范围就是这个目录,**默认包含**其中每个模块
ENGINE_PACKAGE_DIR = "attribution"

# 例外清单:数据集专属的脚本,允许出现具体词汇
EXCLUDED_MODULES: tuple[str, ...] = (
    # 数据集专属的验收脚本:拿真实数据核对「华东→上海→STORE_S0001」这类标准答案,
    # 里面必然写满 gmv / STORE_S0001 / 上海徐家汇旗舰店。它是数据集的验收方,不是引擎。
    "attribution/self_test.py",
)

# 可能挂 docstring 的节点类型
_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 含 CJK 的字面量整串入集:中文没有词边界,按词切分抓不住「上海徐家汇旗舰店」
# 这类实体值(与 conftest 的实体值收集同一口径);长短无关——只有字面量整串恰好
# 等于某个实体值时才会命中,提示文案整句与实体名不等,不会误伤
_CJK_RE = re.compile(r"[一-鿿]")


def discover_engine_modules() -> tuple[str, ...]:
    """attribution/ 下**除例外清单外**的全部模块,返回相对项目根的 posix 路径。"""
    package = PROJECT_ROOT / ENGINE_PACKAGE_DIR
    excluded = {PROJECT_ROOT / name for name in EXCLUDED_MODULES}
    return tuple(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in sorted(package.rglob("*.py"))
        if path not in excluded
    )


# 扫描对象:导入期确定,供 parametrize 生成用例
ENGINE_MODULES: tuple[str, ...] = discover_engine_modules()


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """收集 docstring 对应的字符串常量节点 id,供字面量收集时剔除。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS):
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        first_statement = node.body[0]
        if isinstance(first_statement, ast.Expr) and isinstance(
            first_statement.value, ast.Constant
        ):
            ids.add(id(first_statement.value))
    return ids


def _collect_identifiers(tree: ast.AST) -> set[str]:
    """收集源码里的标识符:变量/属性/函数与类名/形参与关键字参数名。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.arg, ast.keyword)) and node.arg:
            names.add(node.arg)
    return names


def _collect_literal_words(tree: ast.AST, docstring_ids: set[int]) -> set[str]:
    """收集字符串字面量里的词(剔除 docstring)。

    按词切分抓住 `f'SUM({F}.amount)'` 之外的形态——例如有人把整条 SQL 写成
    普通字面量 `'SUM(o.amount)'`;含 CJK 的字面量**整串**入集:中文没有词边界,
    切词抓不住中文实体值,整串才能与词汇表里的实体值对上(见 _CJK_RE)。
    """
    words: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstring_ids:
            continue
        words.update(_WORD_RE.findall(node.value))
        stripped = node.value.strip()
        if stripped and _CJK_RE.search(stripped):
            words.add(stripped)
    return words


def _scan_module(path: Path) -> tuple[set[str], set[str]]:
    """解析单个模块,返回 (标识符集合, 字面量词集合)。"""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    docstring_ids = _docstring_node_ids(tree)
    return _collect_identifiers(tree), _collect_literal_words(tree, docstring_ids)


def _find_violations(path: Path, vocabulary: set[str]) -> list[str]:
    """点名该文件里出现的每个数据集词汇(标识符/字面量分开报,便于定位)。"""
    identifiers, literal_words = _scan_module(path)
    violations = [f"{path}: 标识符 {word!r}" for word in sorted(vocabulary & identifiers)]
    violations += [
        f"{path}: 字符串字面量 {word!r}" for word in sorted(vocabulary & literal_words)
    ]
    return violations


def _strings(value: Any) -> set[str]:
    """把 str / list[str] 字段统一成字符串集合。"""
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {item for item in value if isinstance(item, str)}
    return set()


def _declared_names_from_yaml() -> set[str]:
    """语义层 YAML 里的一等实体名:指标/维度/表名/分解因子/日历名。

    刻意不复用 conftest 的收集实现:两边独立走一遍,才能真正暴露「漏收某一类」。
    """
    names: set[str] = set()
    for path in discover_semantic_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        names |= {str(key) for key in (data.get("metrics") or {})}
        names |= {str(key) for key in (data.get("dimensions") or {})}
        names |= _strings(data.get("fact_table"))
        for dimension in (data.get("dimensions") or {}).values():
            if isinstance(dimension, dict):
                names |= _strings(dimension.get("table"))
        for entry in data.get("decompositions") or []:
            if isinstance(entry, dict):
                names |= _strings(entry.get("factors"))
        calendar = (data.get("time") or {}).get("calendar") or {}
        names |= {str(key) for key in calendar}
    return names


def test_semantic_vocabulary_is_not_empty() -> None:
    """防回退测试的前提:词汇表必须真的收到东西,否则它永远通过。"""
    vocabulary = collect_vocabulary_from_all_datasets()
    assert vocabulary, "词汇表为空——语义层扫描路径失效,防回退测试会退化成假绿"


def test_vocabulary_covers_declared_entities() -> None:
    """词汇表必须覆盖语义层声明的一等实体名(表名/分解因子/日历名等)。"""
    vocabulary = collect_vocabulary_from_all_datasets()
    missing = _declared_names_from_yaml() - vocabulary
    assert not missing, (
        f"词汇表漏收语义层声明的名字(引擎写死它们将不被发现):{sorted(missing)}"
    )


def test_engine_scan_covers_every_module_by_default() -> None:
    """扫描集合 = 包内全部模块 − 例外清单:任何模块都不能悄悄逃逸。"""
    package = PROJECT_ROOT / ENGINE_PACKAGE_DIR
    all_modules = {
        path.relative_to(PROJECT_ROOT).as_posix() for path in package.rglob("*.py")
    }
    assert all_modules, f"{package} 下没有模块——扫描路径失效,防回退测试会退化成假绿"
    assert set(ENGINE_MODULES) == all_modules - set(EXCLUDED_MODULES)


def test_excluded_modules_exist() -> None:
    """例外清单必须指向真实文件:防止清单腐化成「登记了但早已改名」的死条目。"""
    for relative_path in EXCLUDED_MODULES:
        assert (PROJECT_ROOT / relative_path).is_file(), (
            f"例外清单里的 {relative_path} 不存在——"
            "死条目会让人误以为某个模块被处理过,实则是漏扫"
        )


@pytest.mark.parametrize("relative_path", ENGINE_MODULES)
def test_engine_has_no_hardcoded_vocabulary(relative_path: str) -> None:
    """引擎代码里不得出现任何具体数据集的词汇。"""
    vocabulary = collect_vocabulary_from_all_datasets()
    assert vocabulary, "词汇表为空——语义层扫描路径失效,防回退测试会退化成假绿"

    path = PROJECT_ROOT / relative_path
    assert path.is_file(), f"待检查文件不存在:{path}"

    violations = _find_violations(path, vocabulary)
    assert not violations, (
        f"{relative_path} 出现硬编码数据集词汇(应改从语义层读取):\n  "
        + "\n  ".join(violations)
    )
