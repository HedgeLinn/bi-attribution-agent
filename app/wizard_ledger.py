# -*- coding: utf-8 -*-
"""向导的账本:每一步的答案,以及每一步控件的原值 —— 都为了扛住 Streamlit 的 widget 清理。

Streamlit 每次 run 结束时会清掉**本次没有渲染的 widget**的 session_state。向导有四步,
任何时刻只渲染其中一步,于是每一次翻页都在清理上一步的控件。同一件事造成两个方向的错:

    往后走(第 ② 步改了口径 → 走到第 ④ 步 → 落位):第 ② 步的控件早已不存在,落位时读不到
    用户的选择,地图里留下的还是机器判的半可加 —— 而界面从头到尾看起来都正常。
    往后退(第 ② 步改了口径 → 走到第 ③ 步 → 退回第 ② 步):控件被**重建**,而 index=/value=
    只在「控件第一次出现」时生效,于是它退回草稿值;紧接着的记账又把这份草稿值当成用户的
    选择记下来 —— 用户改过的东西就这样被静默抹掉,而且是在他自己眼前(界面显示的就是退回
    后的样子)。

所以账本记两份:

    wz_answers  各步的答案(给 harness 落位用)
    wz_values   各步控件的原值(翻回那一步时回填,见 restore)

只记前者挡不住「往后退」:从答案倒推控件值要给文本控件做一套反向序列化(日历条目、恒等式
因子都得把结构化数据再拼回字符串),而原值回填一个字都不用拼。两份都是普通 dict、不是
widget key —— 这正是它们不会被清理的原因(它们必须带 wz_ 前缀,好让退出向导时被一起清掉)。

本模块零 Streamlit(只认 MutableMapping),可以脱离运行时单测。
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence

__all__ = ["ANSWERS_KEY", "VALUES_KEY", "answers", "record", "restore", "values"]

# 账本键(带 wz_ 前缀:import_wizard._close 按前缀整片清理)
ANSWERS_KEY = "wz_answers"
VALUES_KEY = "wz_values"


def answers(state: Mapping) -> dict:
    """落位时用的答案:各步渲染时记下来的合并结果。

    **没渲染过的步骤不在账本里** —— 缺失即「跳过」,由 import_answers.apply_answers 沿用
    草稿值,这正是本流程的既定语义(空账本等价于「每一步都跳过」)。
    """
    return dict(state.get(ANSWERS_KEY) or {})


def values(state: Mapping) -> dict:
    """控件原值账本:翻回某一步时用来把控件填回用户当时的样子(见 restore)。"""
    return dict(state.get(VALUES_KEY) or {})


def record(state: MutableMapping, keys: Sequence[str], step_answers: Mapping) -> None:
    """渲染完某一步之后调用:记下这一步的答案与控件原值。

    values 只记 `state` 里**确实存在**的键:这一步没渲染过的控件(比如指标被删掉后留下的
    下标)不该凭空写进账本,否则回填时会造出一个用户从没见过的值。
    """
    _merge(state, ANSWERS_KEY, step_answers)
    _merge(state, VALUES_KEY, {key: state[key] for key in keys if key in state})


def restore(state: MutableMapping, keys: Sequence[str]) -> None:
    """渲染某一步之前调用:把账本里的原值填回控件键(只填**缺失**的键)。

    键还在,说明控件这一轮渲染过、值就是用户当下看到的,绝不覆盖;键没了,说明是翻页
    回来重建的 —— 不填就会退回草稿值,再被下一次 record 记成用户的选择。
    """
    ledger = values(state)
    for key in keys:
        if key in ledger and key not in state:
            state[key] = ledger[key]


def _merge(state: MutableMapping, key: str, part: Mapping) -> None:
    """把一部分答案并进账本(按顶层键合并:每一步各自负责自己那几个顶层键)。"""
    merged = dict(state.get(key) or {})
    merged.update(part)
    state[key] = merged