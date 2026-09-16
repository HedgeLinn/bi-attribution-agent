"""确认向导的**纯逻辑**层:答案怎么从界面状态成形,以及它绝不越权替用户做决定。

为什么给 app/ 下这个模块单测:向导的界面部分(4 步控件)薄到不值得跑 AppTest,而
「控件状态 -> 答案 dict」是一层真逻辑,错了**界面上完全看不出来** —— 用户选了「加法」、
地图里写着半可加,两边的显示都正常,只有数字差几十倍。本文件钉的就是这层。

两个反复出现的不变量:
    ① 缺失即跳过:state 里没有键 == 这一步没渲染过 == 沿用草稿值(界面不替用户决定)
    ② 显式选择压倒一切:用户选了什么就是什么,草稿的类型绝不能把它盖回去
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import wizard_common as wc  # noqa: E402
import wizard_ledger as wl  # noqa: E402
import wizard_steps  # noqa: E402
from harness.import_pending import PendingPackage  # noqa: E402
from importer_ui import _signature  # noqa: E402

# 草稿:两个指标,一个机器判成加法、一个判成半可加(锯齿数据的典型形态)
_DRAFT = {
    "fact_table": "fact",
    "date_field": "date",
    "dataset_version": "import",
    "metrics": {
        "amount_sum": {"label": "amount", "expression": "SUM(amount)", "type": "additive"},
        "balance_sum": {"label": "balance", "expression": "SUM(balance)",
                        "type": "semi_additive", "time_aggregation": "last"},
    },
    "dimensions": {"channel": {"label": "channel", "type": "derived",
                               "hierarchy": ["channel"]}},
}

_STATS = {
    "fact": {
        "date": {"type": "DATE", "monotonic": False},
        "channel": {"type": "VARCHAR", "monotonic": False},
        "amount": {"type": "BIGINT", "monotonic": False},
        "balance": {"type": "BIGINT", "monotonic": True},     # 单调列:半可加误判的源头
    }
}


def _pkg(tmp_path: Path, stats: dict | None = None) -> PendingPackage:
    """一个待确认包(纯逻辑测试不碰真实文件,只给路径形状)。"""
    root = tmp_path / ".pending" / "upload_demo"
    return PendingPackage(id="upload_demo", title="demo", root=root,
                          data_dir=root / "data", draft=dict(_DRAFT),
                          stats=_STATS if stats is None else stats)


def _run(pkg: PendingPackage, state: dict, step: int, values: dict | None = None) -> None:
    """跑一次「Streamlit 的 run」,顺序与 render_step 一致。

    ① 上一个 run 里没渲染的控件键已被清掉(只留两本账)
    ② 前端这一次送来的新值
    ③ restore 把账本里的原值填回缺失的控件键
    ④ record 把本步的答案与原值记进账本
    """
    for key in [key for key in state if key not in (wl.ANSWERS_KEY, wl.VALUES_KEY)]:
        state.pop(key)
    state.update(values or {})
    keys = wc.step_keys(pkg, step)
    wl.restore(state, keys)
    wl.record(state, keys, wizard_steps.collect_step(pkg, state, step))


def _frozen_after(pkg: PendingPackage, steps: list) -> dict:
    """按顺序渲染若干步,返回最后的账本。"""
    state: dict = {}
    for values, step in steps:
        _run(pkg, state, step, values)
    return wl.answers(state)


# ---------------------------------------------------------------------------
# ① 显式选择压倒草稿
# ---------------------------------------------------------------------------
def test_additive_label_wins_over_draft_semi_additive():
    """用户选「加法」,地图就必须是加法 —— 哪怕草稿判的是半可加。

    写成「非半可加即 default」会让这一下被 default 悄悄吃掉:界面上选择看着好好的,
    地图里仍是 semi_additive + last,整窗值差几十倍而没有任何报错。
    """
    assert wc.label_to_type(wc.LABEL_ADDITIVE, "semi_additive") == "additive"
    assert wc.label_to_type(wc.LABEL_SEMI, "additive") == "semi_additive"
    assert wc.label_to_type("", "semi_additive") == "semi_additive"   # 认不出来才用兜底


def test_type_to_label_marks_semi_additive():
    assert wc.type_to_label("semi_additive") == wc.LABEL_SEMI
    assert wc.type_to_label("additive") == wc.LABEL_ADDITIVE
    assert wc.type_to_label("derived") == wc.LABEL_ADDITIVE    # 本轮不产出的类型给可改的默认


def test_collect_keeps_the_type_the_user_picked(tmp_path):
    """端到端:把第二个指标(balance_sum)在界面上改成加法,答案里就得是加法。"""
    answers = wizard_steps.collect_step(_pkg(tmp_path), {"wz_m1_type": wc.LABEL_ADDITIVE}, 2)
    specs = {spec["name"]: spec for spec in answers["metrics"]}
    assert specs["balance_sum"]["type"] == "additive"
    assert specs["amount_sum"]["type"] == "additive"


def test_freeze_carries_earlier_steps_past_widget_cleanup(tmp_path):
    """**核心回归**:走到第 ④ 步时,第 ② 步的选择必须还在。

    Streamlit 每次 run 结束都会清掉「本次没渲染的 widget」—— 在第 ④ 步那一次 run 里,
    第 ② 步的类型下拉根本没有被渲染,它的值已经没了。所以账本必须在**渲染那一步时**
    就记下来。少了这本账,用户在界面上改的加法会在落位时变回机器判的半可加,
    而两边看上去都很正常。
    """
    answers = _frozen_after(_pkg(tmp_path), [
        ({wc.metric_key(1, "type"): wc.LABEL_ADDITIVE}, 2),   # ② 步:用户把 balance 改成加法
        ({wc.KEY_CAVEATS: "口径陷阱"}, 4),                     # ④ 步:此刻 ② 的控件已被清理
    ])
    specs = {spec["name"]: spec for spec in answers["metrics"]}
    assert specs["balance_sum"]["type"] == "additive"
    assert answers["caveats"] == ["口径陷阱"]


def test_returning_to_a_step_restores_the_users_choice(tmp_path):
    """**核心回归之二**:翻回第 ② 步,控件里必须还是用户改过的加法,不是草稿的半可加。

    账本只保护「往后走」不够:翻回某一步时控件是**重建**的,而 radio 的 index= / text_area
    的 value= 只在「控件第一次出现」时生效 —— 它们会退回草稿值,紧接着的 record 又把这份
    草稿值当成用户的选择写进账本。用户改过的东西就这样在他眼前被抹掉(界面显示的正是退回
    后的样子,看不到任何异常)。
    """
    pkg = _pkg(tmp_path)
    state: dict = {}
    _run(pkg, state, 2, {wc.metric_key(1, "type"): wc.LABEL_ADDITIVE,
                         wc.metric_key(1, "unit"): "元"})
    _run(pkg, state, 3)                                   # 翻到第 ③ 步:② 的控件被清掉
    assert wc.metric_key(1, "type") not in state
    _run(pkg, state, 2)                                   # 再翻回来
    assert state[wc.metric_key(1, "type")] == wc.LABEL_ADDITIVE
    assert state[wc.metric_key(1, "unit")] == "元"
    specs = {spec["name"]: spec for spec in wl.answers(state)["metrics"]}
    assert specs["balance_sum"]["type"] == "additive"
    assert specs["balance_sum"]["unit"] == "元"


class _Watched(dict):
    """state 替身:把「收集时读过哪些键」记下来。"""

    def __init__(self, data):
        super().__init__(data)
        self.read: set = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)


@pytest.mark.parametrize("step", [1, 2, 3, 4])
def test_step_keys_covers_everything_collect_reads(tmp_path, step):
    """记账范围(step_keys)必须覆盖收集时读到的**每一个**控件键。

    两边对不上的后果是静默的:某个键没进账本 -> 翻页回来不回填 -> 控件退回草稿值 ->
    record 又把它记成用户的选择。所以这条检查是机械的,不靠人记得同步两处。
    """
    pkg = _pkg(tmp_path)
    state = _Watched({})
    wizard_steps.collect_step(pkg, state, step)
    allowed = set(wc.step_keys(pkg, step)) | {wl.ANSWERS_KEY, wl.VALUES_KEY}
    assert state.read <= allowed


# ---------------------------------------------------------------------------
# ② 缺失即跳过
# ---------------------------------------------------------------------------
def test_collect_defaults_to_draft_when_nothing_rendered(tmp_path):
    """一步都没渲染(用户连点三次「下一步」):答案不能改变草稿的任何口径。

    这是「跳过 = 不写字段」的实现保证 —— 空答案走一遍 apply_answers 后,类型必须逐一
    等于草稿原值,而不是被界面默认成加法。
    """
    from harness.import_answers import apply_answers

    final = apply_answers(_DRAFT, wl.answers({}))
    assert final["metrics"]["amount_sum"]["type"] == "additive"
    assert final["metrics"]["balance_sum"]["type"] == "semi_additive"
    assert final["metrics"]["balance_sum"]["time_aggregation"] == "last"


def test_collect_removes_metric_only_when_unchecked(tmp_path):
    """取消勾选 = 从地图里删掉;没渲染过(键不存在)默认保留。"""
    answers = wizard_steps.collect_step(_pkg(tmp_path), {"wz_m0_keep": False}, 2)
    specs = {spec["name"]: spec for spec in answers["metrics"]}
    assert specs["amount_sum"]["keep"] is False
    assert specs["balance_sum"]["keep"] is True


def test_collect_reads_dimensions_only_when_rendered(tmp_path):
    """第 ③ 步没渲染过就不写 dimensions —— 写了空列表会把草稿的维度整片清掉。"""
    pkg = _pkg(tmp_path)
    assert "dimensions" not in wizard_steps.collect_step(pkg, {}, 3)
    assert wizard_steps.collect_step(pkg, {"wz_dims": ["channel"]}, 3)["dimensions"] == ["channel"]


def test_collect_builds_calendar_from_name_and_entries(tmp_path):
    """日历:一个名称 + 若干条目;名或条目为空就不写进地图(不造默认名)。"""
    state = {"wz_cal_name": "大促",
             "wz_cal_text": "618|2026-06-01|2026-06-18|大促脉冲\n双11|2026-11-01|2026-11-11"}
    calendar = wizard_steps.collect_step(_pkg(tmp_path), state, 4)["calendar"]
    assert calendar["大促"][0] == {"name": "618", "range": ["2026-06-01", "2026-06-18"],
                                   "note": "大促脉冲"}
    assert calendar["大促"][1]["name"] == "双11"
    assert "calendar" not in wizard_steps.collect_step(_pkg(tmp_path), {"wz_cal_name": "大促"}, 4)


# ---------------------------------------------------------------------------
# 文本控件 -> 结构化答案
# ---------------------------------------------------------------------------
def test_answer_lines_drops_blank_lines():
    assert wc.answer_lines(" 一句 \n\n  另一句\n") == ["一句", "另一句"]


def test_answer_pairs_keeps_incomplete_rows_for_the_backend_to_reject():
    """日期给多给少都原样交出去:界面替后端吞掉,用户会以为填对了。"""
    items = wc.answer_pairs("618|2026-06-01|2026-06-18\n残缺|2026-06-01")
    assert items[0]["range"] == ["2026-06-01", "2026-06-18"]
    assert items[1]["range"] == ["2026-06-01"]          # 只有一天,交给 _iso_span 去报错
    assert wc.answer_pairs("\n  \n") == []


def test_gather_decompositions_accepts_commas_and_newlines():
    state = {"wz_dec0_target": "mrr", "wz_dec0_factors": "流A, 流B，流C"}
    assert wc.gather_decompositions(state, 2) == [
        {"target": "mrr", "factors": ["流A", "流B", "流C"]}]


def test_gather_decompositions_drops_half_filled_rows():
    """填了一半的行当没填 —— 写一份注定不合法的声明只会让用户去猜错在哪。"""
    assert wc.gather_decompositions({"wz_dec0_target": "mrr"}, 1) == []
    assert wc.gather_decompositions({"wz_dec0_factors": "a,b"}, 1) == []


# ---------------------------------------------------------------------------
# 拆分的记账
# ---------------------------------------------------------------------------
def test_live_splits_ignores_broken_state():
    assert wc.live_splits({}, "wz_m0_splits") == []
    assert wc.live_splits({"wz_m0_splits": "字符串不是列表"}, "wz_m0_splits") == []
    assert wc.live_splits({"wz_m0_splits": [{"name": ""}]}, "wz_m0_splits") == []


def test_live_splits_returns_copies():
    """拿到的是副本:界面删一项不该顺着 list 改到 session_state 里的原对象。"""
    state = {"wz_m0_splits": [{"name": "a", "column": "c", "values": ["v"]}]}
    item = wc.live_splits(state, "wz_m0_splits")[0]
    item["name"] = "改了"
    assert state["wz_m0_splits"][0]["name"] == "a"


def test_split_meta_names_each_child():
    meta = wc.split_meta([{"name": "amount_douyin", "column": "channel", "values": ["抖音"]}])
    assert meta == ["amount_douyin(按 channel=['抖音'])"]


# ---------------------------------------------------------------------------
# 列与判据
# ---------------------------------------------------------------------------
def test_column_names_prefers_stats_and_falls_back_to_parquet(tmp_path):
    """两条路都要有:导入路径带 stats,补确认路径(已落位的数据集)只能现场 DESCRIBE。"""
    pkg = _pkg(tmp_path)
    assert wc.column_names(pkg) == ["date", "channel", "amount", "balance"]
    pkg.data_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": ["2026-05-01"], "amount": [1]}).to_parquet(
        pkg.data_dir / "fact.parquet")
    assert wc.column_names(_pkg(tmp_path, stats={})) == ["amount", "date"]


def test_table_names_comes_from_stats(tmp_path):
    assert wc.table_names(_pkg(tmp_path)) == ["fact"]


def test_column_hint_reports_the_monotonic_column(tmp_path):
    """半可加的判据必须能说出来:列单调正是锯齿数据被误判的机制。"""
    assert "单调" in wc.column_hint(_pkg(tmp_path), "SUM(balance)")
    assert wc.column_hint(_pkg(tmp_path), "SUM(amount)") == ""
    assert wc.column_hint(_pkg(tmp_path), "SUM(a)/SUM(b)") == ""    # 不是单个 SUM:给不出判据


def test_metric_rows_prefers_state_then_falls_back_to_draft(tmp_path):
    rows = {row["name"]: row for row in wc.metric_rows(_pkg(tmp_path), {})}
    assert rows["balance_sum"]["type_label"] == wc.LABEL_SEMI       # 首次进来显示机器判断
    assert rows["balance_sum"]["hint"]                              # 并说明判据
    again = {row["name"]: row for row in
             wc.metric_rows(_pkg(tmp_path), {wc.metric_key(1, "type"): wc.LABEL_ADDITIVE})}
    assert again["balance_sum"]["type_label"] == wc.LABEL_ADDITIVE  # 用户改过就显示他的选择
    assert again["amount_sum"]["unit"] == ""


# ---------------------------------------------------------------------------
# 上传指纹(修掉「每次 rerun 都重新导入」的那个死循环)
# ---------------------------------------------------------------------------
class _Upload:
    """st.file_uploader 的替身:只需要 name 与 getvalue()。"""

    def __init__(self, name: str, data: bytes):
        self.name, self._data = name, data

    def getvalue(self) -> bytes:
        return self._data


def test_signature_is_stable_for_the_same_bytes_and_differs_otherwise():
    """同一份字节必须给出同一指纹 —— 成功路径会 rerun,而 rerun 送回来的是同一个文件。

    指纹不稳定 = 每 rerun 一次就重新导入一遍,暂存区里堆满同一个文件的副本,页面停不下来。
    """
    first = _signature(_Upload("a.csv", b"date,amount\n2026-05-01,1\n"))
    same = _signature(_Upload("a.csv", b"date,amount\n2026-05-01,1\n"))
    other = _signature(_Upload("a.csv", b"date,amount\n2026-05-01,2\n"))
    assert first == same
    assert first != other


@pytest.mark.parametrize("name", ["b.csv"])
def test_signature_includes_the_file_name(name):
    assert _signature(_Upload("a.csv", b"x")) != _signature(_Upload(name, b"x"))