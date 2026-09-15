"""工具 schema 动态化的验收:两份不同的语义层必须产出不同的 schema 与提示词。

契约:docs/REUSE_DESIGN.md §3.4(工具与提示词动态化)/ §3.7(前端切语义)

**核心断言一句话:换语义层 = 换模型看到的工具。**
所以本文件拿一份「3 指标 2 维度」的合成语义层(内联 YAML + tmp_path)与真实数据集的
语义层对照:参数枚举、工具描述里的清单、系统提示词的数据集段落都必须跟着变。
任何一处写死(写死的 Literal、写死的指标清单、写死的日期范围)都会在这里失败。

合成语义层的词汇(zone / segment / signups ...)刻意与真实数据集不重合:
只有不重合,「枚举里不含对方的维度名」才是有信息的断言。
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from attribution.semantic import Semantic
from harness import context, tools

# tests/ 是常规包(有 __init__.py),测试模块之间的共享一律走包内导入
from tests.conftest import collect_vocabulary_from_all_datasets, discover_semantic_files
# 复用现有防回退测试的扫描器(标识符 + 字符串字面量,docstring 除外)
from tests.test_engine_has_no_hardcoded_vocabulary import _find_violations

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 渲染工具 schema 与提示词的模块:必须零数据集词汇(§3.6⑤ 的自觉版——
# 那份机械测试只扫 attribution/,扫不到 harness/)
HARNESS_PROMPT_MODULES: tuple[str, ...] = ("harness/tools.py", "harness/context.py",
                                           "harness/tool_catalog.py")

# 合成语义层:3 指标、2 维度、1 个非 promos 名字的日历、1 条口径陷阱。
# 日历刻意不叫 promos,以证明 harness 是通用遍历而不是按名字取。
SYNTHETIC_YAML = """schema_version: "2.0"
dataset: synthetic-demo
dataset_version: "0.1.0"
fact_table: fact_events
date_field: event_day
metrics:
  signups:
    label: 注册数
    expression: COUNT(DISTINCT user_ref)
    type: additive
    depends_on: [user_ref]
  tickets:
    label: 工单数
    expression: SUM(ticket_cnt)
    type: additive
    depends_on: [ticket_cnt]
  cost_per_signup:
    label: 单注册成本
    expression: spend / NULLIF(signups, 0)
    type: derived
    depends_on: [spend, signups]
dimensions:
  zone:
    label: 区域
    table: dim_zone
    key: zone_id
    name_column: zone_name
    hierarchy: [continent, zone_id]
  segment:
    label: 客群
    table: dim_segment
    key: segment_id
    hierarchy: [segment_group, segment_id]
time:
  calendar:
    campaign:
      - name: "春季活动"
        range: ["2031-03-01", "2031-03-10"]
        note: "活动期内的峰值属预期"
caveats:
  - "合成陷阱:两个来源的口径不一致,不能直接相加"
"""

# 合成语义层渲染出的日期范围(与真实数据集的区间无关)
SYNTHETIC_SPAN = ("2031-01-01", "2031-12-31")


# ---------------------------------------------------------------------------
# 夹具与工具函数
# ---------------------------------------------------------------------------
@pytest.fixture
def synthetic(tmp_path) -> Semantic:
    """合成语义层:内联 YAML 写到 tmp_path 后加载,不依赖任何真实数据集文件。"""
    path = tmp_path / "semantic.yaml"
    path.write_text(SYNTHETIC_YAML, encoding="utf-8")
    return Semantic.load(str(path))


@pytest.fixture
def synthetic_tools(synthetic) -> dict:
    """按合成语义层生成的工具集:name -> StructuredTool。"""
    return _by_name(tools.build_tools(synthetic))


def _by_name(tool_list) -> dict:
    return {tool.name: tool for tool in tool_list}


def _real_layers() -> list[tuple[Path, Semantic]]:
    """真实数据集的语义层(两种布局都能找到:semantic/ 与 datasets/*/)。"""
    return [(path, Semantic.load(str(path))) for path in discover_semantic_files()]


def _enum_of(tool, field: str) -> set[str]:
    """取某个工具参数的可选值域:动态 schema 里的 Literal 枚举。"""
    return set(get_args(tool.args_schema.model_fields[field].annotation))


def _rendered_line(name: str) -> str:
    """清单渲染的单行前缀(如 '  - signups('):按行断言可避开子串误判。"""
    return f"  - {name}("


# ---------------------------------------------------------------------------
# 关键验收:枚举来自语义层
# ---------------------------------------------------------------------------
def test_enums_come_from_the_semantic_layer_they_were_built_from(synthetic) -> None:
    """metric / dimension 的可选值域 === 该语义层的指标名 / 维度名(以真实层为对照)。"""
    layers = _real_layers()
    assert layers, f"未找到任何真实语义层:{list(discover_semantic_files())}"
    for path, layer in [(Path("<synthetic>"), synthetic), *layers]:
        made = _by_name(tools.build_tools(layer))
        assert _enum_of(made[tools.CONTRIBUTE_TOOL], "metric") == set(layer.metrics), path
        assert _enum_of(made[tools.CONTRIBUTE_TOOL], "dimension") == set(layer.dimensions), path
        assert _enum_of(made[tools.ANOMALY_TOOL], "metric") == set(layer.metrics), path
        assert _enum_of(made[tools.QUERY_TOOL], "metric") == set(layer.metrics), path


def test_enums_of_two_layers_do_not_leak_into_each_other(synthetic) -> None:
    """关键验收:合成语义层的枚举里不含真实数据集的指标/维度,反之亦然。"""
    synthetic_enum = {
        field: _enum_of(_by_name(tools.build_tools(synthetic))[tools.CONTRIBUTE_TOOL], field)
        for field in ("metric", "dimension")
    }
    for path, layer in _real_layers():
        made = _by_name(tools.build_tools(layer))
        real_enum = {field: _enum_of(made[tools.CONTRIBUTE_TOOL], field)
                     for field in ("metric", "dimension")}
        for field, declared in (("metric", layer.metrics), ("dimension", layer.dimensions)):
            leaked = synthetic_enum[field] & set(declared)
            assert not leaked, f"{path} 的{field}漏进了合成语义层的工具 schema:{sorted(leaked)}"
            assert not (real_enum[field] & set(getattr(synthetic, field + "s"))), \
                f"{path} 的枚举里出现了合成语义层的{field}"


def test_two_layers_yield_different_tool_schemas(synthetic) -> None:
    """同一个工具、两份语义层 -> 参数 schema 必须不同(枚举是最直接的差异)。"""
    made_from_synthetic = _by_name(tools.build_tools(synthetic))
    differing: set[str] = set()
    for _path, layer in _real_layers():
        made_from_real = _by_name(tools.build_tools(layer))
        assert set(made_from_synthetic) == set(made_from_real), "工具名集合必须稳定"
        for name, tool_obj in made_from_synthetic.items():
            if tool_obj.args_schema.model_json_schema() != made_from_real[name].args_schema.model_json_schema():
                differing.add(name)
    # 取指标/维度的四个工具随语义层变(decompose 的 target 枚举来自分解声明);
    # 概览工具无参数,描述也不含数据集词汇
    assert differing == {tools.ANOMALY_TOOL, tools.CONTRIBUTE_TOOL, tools.QUERY_TOOL,
                         tools.DECOMPOSE_TOOL}, (
        f"两份语义层生成的 schema 差异不符合预期:{sorted(differing)}——枚举多半是写死的"
    )


def test_tool_names_and_args_are_stable(synthetic_tools) -> None:
    """工具名与参数名是对模型与引擎的稳定契约,不因 schema 动态化而漂移。"""
    assert list(synthetic_tools) == [
        tools.OVERVIEW_TOOL, tools.ANOMALY_TOOL, tools.CONTRIBUTE_TOOL, tools.QUERY_TOOL,
        tools.DECOMPOSE_TOOL,
    ]
    assert set(synthetic_tools[tools.CONTRIBUTE_TOOL].args_schema.model_fields) == {
        "metric", "dimension", "level", "base_start", "base_end",
        "cmp_start", "cmp_end", "top_k", "filters",
    }
    assert set(synthetic_tools[tools.QUERY_TOOL].args_schema.model_fields) == {
        "metric", "dims", "filters", "start", "end",
    }
    assert set(synthetic_tools[tools.DECOMPOSE_TOOL].args_schema.model_fields) == {
        "target", "base_start", "base_end", "cmp_start", "cmp_end",
        "dimension", "level", "filters", "top_k",
    }


# ---------------------------------------------------------------------------
# 工具描述(docstring)由语义层渲染
# ---------------------------------------------------------------------------
def test_tool_doc_renders_metric_catalog(synthetic, synthetic_tools) -> None:
    """docstring 里必须有本语义层的指标清单,且不含别的数据集的指标。"""
    doc = synthetic_tools[tools.CONTRIBUTE_TOOL].description
    for name, metric in synthetic.metrics.items():
        assert _rendered_line(name) in doc, f"工具描述缺少指标 {name}"
        assert metric.label in doc, f"工具描述缺少指标 {name} 的展示名"
    for path, layer in _real_layers():
        for name in layer.metrics:
            assert _rendered_line(name) not in doc, f"工具描述里出现了 {path} 的指标 {name}"


def test_tool_doc_renders_hierarchy_and_filter_rules(synthetic, synthetic_tools) -> None:
    """层级与「过滤要用 ID 而不是显示名」的规则也从语义层渲染(字段名来自语义层)。"""
    doc = synthetic_tools[tools.QUERY_TOOL].description
    for dim in synthetic.dimensions.values():
        assert " -> ".join(dim.hierarchy) in doc, f"工具描述缺少维度 {dim.name} 的层级"
    keyed = [d for d in synthetic.dimensions.values() if d.key]
    named = [d for d in synthetic.dimensions.values() if d.name_column]
    assert keyed and named, "合成语义层必须同时有主键字段与展示名列,否则规则渲染无从验证"
    assert all(d.key in doc for d in keyed), "过滤规则里没列出主键字段"
    assert all(d.name_column in doc for d in named), "过滤规则里没列出展示名列"


def test_overview_tool_delegates_to_semantic_service(synthetic) -> None:
    """概览工具的正文由 Semantic.render_overview() 现场生成,不在 harness 里重复渲染。"""
    overview = tools.build_tools(synthetic)[0]
    assert overview.name == tools.OVERVIEW_TOOL
    assert overview.func() == synthetic.render_overview()


# ---------------------------------------------------------------------------
# 系统提示词:数据集无关的方法论 + 由语义层渲染的数据集上下文
# ---------------------------------------------------------------------------
def test_system_prompt_context_comes_from_semantic_layer(synthetic) -> None:
    """提示词的数据集上下文随语义层变:本层的指标出现,别的数据集的指标不出现。"""
    prompt = context.render_system_prompt(synthetic, context.DatasetContext())
    for name, metric in synthetic.metrics.items():
        assert _rendered_line(name) in prompt, f"提示词缺少指标 {name}"
        assert metric.label in prompt
    for path, layer in _real_layers():
        for name in layer.metrics:
            assert _rendered_line(name) not in prompt, f"提示词里出现了 {path} 的指标 {name}"


def test_date_range_is_probed_not_hardcoded(synthetic) -> None:
    """日期范围是传入的探测结果,不是写死的常量。"""
    prompt = context.render_system_prompt(
        synthetic, context.DatasetContext(date_range=SYNTHETIC_SPAN)
    )
    assert SYNTHETIC_SPAN[0] in prompt and SYNTHETIC_SPAN[1] in prompt
    # 改造前 loop.py 写死的示例区间:任何语义层渲染出的提示词里都不该再出现
    assert "2024-09" not in prompt, "提示词里出现了写死的日期范围"
    assert "2026-08" not in prompt, "提示词里出现了写死的日期范围"
    # 探测不到范围时整段消失,而不是编一个假区间
    assert SYNTHETIC_SPAN[0] not in context.render_system_prompt(synthetic)


def test_calendar_and_caveats_render(synthetic) -> None:
    """促销日历与口径陷阱进提示词:命中日历的回落不该被当异常,口径陷阱要提前说明。"""
    prompt = context.render_system_prompt(
        synthetic,
        context.DatasetContext(caveats=("口径陷阱:A 与 B 不可比",),
                               calendar=(context.CalendarEntry("春季活动", "2031-03-01",
                                                               "2031-03-10", "活动期峰值属预期"),)),
    )
    assert "春季活动" in prompt and "活动期峰值属预期" in prompt
    assert "口径陷阱:A 与 B 不可比" in prompt


def test_dataset_context_loads_calendar_and_caveats_from_yaml(tmp_path) -> None:
    """文档级事实从语义层 YAML 读:日历名不写死,通用遍历即可拿到。"""
    path = tmp_path / "semantic.yaml"
    path.write_text(SYNTHETIC_YAML, encoding="utf-8")
    facts = context.DatasetContext.load(path)
    assert [entry.name for entry in facts.calendar] == ["春季活动"]
    assert (facts.calendar[0].start, facts.calendar[0].end) == ("2031-03-01", "2031-03-10")
    assert facts.caveats == ("合成陷阱:两个来源的口径不一致,不能直接相加",)
    assert facts.date_range is None, "时间范围只能来自数据探测,不能从 YAML 里读"


def test_methodology_flags_data_problems(synthetic) -> None:
    """方法论与数据集无关,且必须要求区分业务问题与数据问题(§5.4 的 C 类坑)。"""
    prompt = context.render_system_prompt(synthetic)
    assert "数据质量" in prompt and "数据问题" in prompt, "缺少「区分业务问题与数据问题」的方法论"
    assert "整体下跌" in prompt, "缺少「不许停在整体下跌」的下钻纪律"
    assert "decompose" in prompt, "缺少机制分解(decompose)的方法论说明"


# ---------------------------------------------------------------------------
# 自觉版防回退:渲染工具 schema 与提示词的代码里不得有数据集词汇
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("relative_path", HARNESS_PROMPT_MODULES)
def test_harness_render_modules_have_no_hardcoded_vocabulary(relative_path: str) -> None:
    """harness/tools.py 与 harness/context.py 里不得出现任何具体数据集的词汇。"""
    vocabulary = collect_vocabulary_from_all_datasets()
    assert vocabulary, "词汇表为空——防回退测试会退化成假绿"

    path = PROJECT_ROOT / relative_path
    assert path.is_file(), f"待检查文件不存在:{path}"
    violations = _find_violations(path, vocabulary)
    assert not violations, (
        f"{relative_path} 出现硬编码数据集词汇(应改为由语义层渲染):\n  "
        + "\n  ".join(violations)
    )


def test_anomaly_tool_cmp_args_are_deprecated_and_optional(synthetic_tools) -> None:
    """契约 v2:异常检测改为日历感知(基线由窗口之前的历史估计),
    cmp_start/cmp_end 弃用——参数保留但可选,工具描述必须说明新基线口径。"""
    anomaly = synthetic_tools[tools.ANOMALY_TOOL]
    fields = anomaly.args_schema.model_fields
    assert fields["cmp_start"].default is None and fields["cmp_end"].default is None
    assert "同星期几" in anomaly.description
    assert "无需指定基期" in anomaly.description
