"""导入向导的纯逻辑层:session_state 子集 <-> harness 认得的答案 dict。

为什么单开一个模块:`app/import_wizard.py` 要画 4 步界面已经够长,而「界面状态怎么变成
答案」是**可单测的逻辑**,不该埋在 st.* 调用之间。本模块**零 Streamlit**,只认 Mapping ——
测试直接喂普通 dict 就能跑,不必启动 Streamlit 运行时。

它也不做任何业务判断:拆分拆不拆得出来、类型合不合法、闸门过不过,全部由
harness/import_metric.py 与 harness/import_confirm.py 说了算。这里只做**搬运**,
把错误留给它们抛(在界面上变成一句可读的报错,而不是静默改数)。

一条必须守住的边界:向导的记账(splits)只活在 session_state 里,**绝不进答案 dict**。
harness 的 tune_metric 用 deepcopy 保留原 entry 的所有键、ordered 也「不丢字段」,
所以任何塞进 metric entry 的私有元数据都会一路漏进 semantic.yaml。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from attribution.sql_source import introspect_columns
from harness.import_metric import (
    TYPE_ADDITIVE,
    TYPE_SEMI_ADDITIVE,
    sum_inner,
)
from harness.import_pending import PendingPackage

# 界面上的类型标签 <-> 语义层的 type。只给两项:本轮不提供自由表达式,比率/派生指标
# 走第 ③ 步的「恒等式」声明(那是 derived,由 depends_on 与表达式共同定义)。
LABEL_ADDITIVE = "加法(逐期求和)"
LABEL_SEMI = "半可加(窗口内取期末值)"

__all__ = [
    "DECOMP_ROWS", "KEY_CAVEATS", "KEY_CAL_NAME", "KEY_CAL_TEXT", "KEY_DATE", "KEY_DIMS",
    "KEY_FACT", "LABEL_ADDITIVE", "LABEL_SEMI", "answer_lines", "answer_pairs",
    "column_hint", "column_names", "dimension_rows", "gather_decompositions",
    "label_to_type", "live_splits", "metric_base", "metric_key", "metric_rows",
    "split_meta", "step_keys", "table_names", "type_to_label",
]


# ---------------------------------------------------------------------------
# 控件键的命名表
# ---------------------------------------------------------------------------
# 界面上的控件键全部在这里定义:渲染(wizard_steps)、记账与回填(wizard_ledger)按同一套
# 名字读写。任何一处另起炉灶都是静默失败 —— 键名对不上时,取值只会拿到 None,而界面
# 看着一切正常。tests/test_import_wizard.py 里有一条「记账范围必须覆盖收集时读到的
# 每一个键」的机械检查钉住这件事。
KEY_FACT = "wz_fact_table"
KEY_DATE = "wz_date_field"
KEY_DIMS = "wz_dims"
KEY_CAVEATS = "wz_caveats"
KEY_CAL_NAME = "wz_cal_name"
KEY_CAL_TEXT = "wz_cal_text"

# 按下标命名的一组(第 ② 步的指标):wz_m<i>_type / wz_m<i>_unit / …
_PREFIX_METRIC = "wz_m"


def metric_base(index: int) -> str:
    """第 ② 步第 index 条指标的键前缀(该行的控件各加 `_type` / `_unit` / …)。"""
    return f"{_PREFIX_METRIC}{index}"


def metric_key(index: int, suffix: str) -> str:
    """第 ② 步第 index 条指标上某个控件的键。"""
    return f"{metric_base(index)}_{suffix}"


# 第 ③ 步的恒等式行数:本轮最多两条(单表下的恒等式本就少见,给太多行只是噪音)。
# 记账范围(step_keys)按它展开,所以行数与渲染必须同源。
DECOMP_ROWS = 2

# 每条指标上属于「用户做过的决定」的控件(拆分编辑器那几个半途的键不在内)
_METRIC_SUFFIXES = ("type", "unit", "keep", "splits")
_DECOMP_SUFFIXES = ("target", "factors")


def step_keys(pkg: PendingPackage, step: int) -> list[str]:
    """第 step 步用到哪些控件键 —— 账本据此划定「本步要记 / 翻回来要填」的范围。

    只列**这一步的**键:拆分编辑器那几个正在编辑的输入(wz_m0_col / _vals / _newname)不在内,
    它们是半途的输入,不是用户已经做过的决定。
    """
    if step <= 1:
        return [KEY_FACT, KEY_DATE]
    if step == 2:
        count = len(pkg.draft.get("metrics") or {})
        return [metric_key(i, suffix) for i in range(count) for suffix in _METRIC_SUFFIXES]
    if step == 3:
        return [KEY_DIMS] + [f"wz_dec{i}_{suffix}"
                             for i in range(DECOMP_ROWS) for suffix in _DECOMP_SUFFIXES]
    return [KEY_CAVEATS, KEY_CAL_NAME, KEY_CAL_TEXT]


# ---------------------------------------------------------------------------
# 文本控件 -> 结构化答案
# ---------------------------------------------------------------------------
def answer_lines(text) -> list[str]:
    """多行文本 -> 去空白、去空行的行列表(caveats 用:一行一条)。"""
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def answer_pairs(text) -> list[dict]:
    """多行文本 -> 日历条目:每行 `名称|起始|终止|备注`(备注可省)。

    只丢「没有名字」的行;日期给多给少都原样交出去 —— 那由后端的 _apply_calendar 判,
    界面替它吞掉反而会让用户以为写对了。
    """
    items = []
    for line in answer_lines(text):
        parts = [part.strip() for part in line.split("|")]
        if not parts[0]:
            continue
        item: dict = {"name": parts[0], "range": parts[1:3]}
        if len(parts) > 3 and parts[3]:
            item["note"] = parts[3]
        items.append(item)
    return items


def gather_decompositions(state: Mapping, count: int) -> list[dict]:
    """第 ③ 步的恒等式行:target = Σ factors。填了一半的行不收(当没填)。

    只组装结构,合法性交给 harness:target / factors 必须是已有指标、因子个数下限、
    ratio 恰两个因子,这些规则地图层已有一份(find_decomposition_problems),不重写。
    """
    rows = []
    for index in range(count):
        target = str(state.get(f"wz_dec{index}_target") or "").strip()
        factors = answer_lines(re.sub(r"[,，]", "\n",
                                     str(state.get(f"wz_dec{index}_factors") or "")))
        if target and factors:
            rows.append({"target": target, "factors": factors})
    return rows


# ---------------------------------------------------------------------------
# 类型标签
# ---------------------------------------------------------------------------
def label_to_type(label, default: str = TYPE_ADDITIVE) -> str:
    """界面标签 -> 语义层 type;只有认不出来(空串 / 意料之外的值)才用默认。

    两个标签都要显式判:**不能写成「非半可加即 default」** —— 用户在界面上明明选了
    「加法」,而 default 带着草稿的 semi_additive,那份地图就会一路错到底,还看不出
    是哪一步错的(选中态看着就是加法)。
    """
    text = str(label or "")
    if text == LABEL_SEMI:
        return TYPE_SEMI_ADDITIVE
    if text == LABEL_ADDITIVE:
        return TYPE_ADDITIVE
    return default


def type_to_label(kind) -> str:
    """语义层 type -> 界面标签;半可加以外的(含 derived)都显示成加法,由用户改。"""
    return LABEL_SEMI if str(kind) == TYPE_SEMI_ADDITIVE else LABEL_ADDITIVE


# ---------------------------------------------------------------------------
# 从草稿与列统计渲染每步的输入
# ---------------------------------------------------------------------------
def table_names(pkg: PendingPackage) -> list[str]:
    """可当事实表的表名:优先列统计里已有的表,读不到就现场扫 parquet。

    单表导入下只有一个候选,界面仍照常渲染它 —— 多表轮次复用同一条路径,不必改。
    """
    if pkg.stats:
        return sorted(str(name) for name in pkg.stats)
    return sorted(introspect_columns(str(pkg.data_dir)))


def column_names(pkg: PendingPackage) -> list[str]:
    """事实表里可引用的列名:优先用导入时算好的列统计,读不到就现场问 DuckDB。

    两条路都要有:待确认包带 stats(导入路径有),而「补确认」读的是已落位的数据集,
    没有 stats(落位时没搬它)—— 那条路上只能现场 DESCRIBE。
    """
    table = str(pkg.draft.get("fact_table") or "")
    columns = pkg.stats.get(table) or {}
    if columns:
        return [str(name) for name in columns]
    return sorted(introspect_columns(str(pkg.data_dir)).get(table) or ())


def column_hint(pkg: PendingPackage, expression: str) -> str:
    """某个指标的自动推断判据(告诉用户「机器为什么这么判」);没有就返回空串。

    唯一的判据来源是列统计里的 monotonic 标志:它正是半可加误判的源头(常量列与
    月内累计列都会 100% / 97.6% 满足它),所以判成半可加时一定要把这句摆出来。
    """
    column = sum_inner(str(expression or ""))
    if column is None:
        return ""
    stat = (pkg.stats.get(str(pkg.draft.get("fact_table") or "")) or {}).get(column) or {}
    if stat.get("monotonic"):
        return f"列 {column} 按日单调(累计型数据),自动推断为半可加"
    return ""


def metric_rows(pkg: PendingPackage, state: Mapping) -> list[dict]:
    """向导第 ② 步的每行输入:草稿里每个指标 + 界面上当前的选择。

    type_label / unit 取 state 优先(用户已经选过的),否则回落到草稿 —— 第一次进来时显示的
    是机器推断的答案(而不是空白)。注意「翻页回来」这条路靠的不是这里:那时 state 里已经
    没有这些键了(wizard_ledger.restore 会在渲染前把它们填回去)。
    """
    metrics = pkg.draft.get("metrics") or {}
    rows = []
    for index, (name, entry) in enumerate(metrics.items()):
        entry = entry or {}
        key = metric_base(index)
        expression = str(entry.get("expression") or "")
        rows.append({
            "key": key,
            "name": str(name),
            "label": str(entry.get("label") or name),
            "expression": expression,
            "type_label": str(state.get(f"{key}_type")
                              or type_to_label(str(entry.get("type") or ""))),
            "unit": str(state.get(f"{key}_unit") or entry.get("unit") or ""),
            "hint": column_hint(pkg, expression),
        })
    return rows


def dimension_rows(pkg: PendingPackage) -> list[dict]:
    """向导第 ③ 步回显:草稿里已有的维度(单表导入只产派生维度,不 join)。

    派生维度**没有 column 字段**,它的下钻字段写在 hierarchy 里(见 harness/import_draft
    的 add_derived_dimensions)。只读 column 会永远读到空 —— 界面上的多选默认一个都不勾,
    用户一路点「下一步」过去,提交时就把草稿的维度整片清掉了。
    """
    rows = []
    for index, (name, entry) in enumerate((pkg.draft.get("dimensions") or {}).items()):
        entry = entry or {}
        hierarchy = [str(field) for field in (entry.get("hierarchy") or [])]
        rows.append({
            "key": f"wz_d{index}",
            "name": str(name),
            "label": str(entry.get("label") or name),
            "column": str(entry.get("column") or (hierarchy[0] if hierarchy else "")),
        })
    return rows


# ---------------------------------------------------------------------------
# 拆分的记账(session_state 里的 list,不进语义层)
# ---------------------------------------------------------------------------
def live_splits(state: Mapping, key: str) -> list[dict]:
    """读回某个父指标下已生成的子项(界面上「拆过哪几刀」)。

    这份记账只活在 session_state 里:它最终以 answers["metrics"][*]["splits"] 的形式
    交给 harness/import_answers.py,由 one_split 现推 depends_on —— 所以「删掉一个子项」
    只要不再把它放进答案即可,不存在需要回头修补的残留字段。

    读回来的每一项都拷成新 dict:界面拿到的是副本,改它不会顺着 list 改到 state。
    """
    raw = state.get(key)
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        return []
    return [dict(item) for item in raw
            if isinstance(item, Mapping) and item.get("name")]


def split_meta(items: Sequence[Mapping]) -> list[str]:
    """回显某个父指标下已生成的拆分(「这个指标被拆过哪几刀」)。"""
    return [f"{item.get('name')}(按 {item.get('column')}={item.get('values')})"
            for item in items]