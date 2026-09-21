# -*- coding: utf-8 -*-
"""BI 归因分析 agent —— Streamlit 前端。

启动方式:
    cd D:\\Project\\chat\\chatv2
    streamlit run app/app.py

对接的后端接口(严格按此调用,不改动后端):
    from harness.loop import run
    run(query, verbose=False, on_event=callback)    # 跑一轮归因分析(同步阻塞)

数据集由侧边栏的「分析语义」下拉选择(app/dataset_selector.py,§3.7):选中即重建
(Semantic → Engine → 工具 schema → 系统提示词);本文件只调用,不持有任何数据集知识。

事件回调签名 on_event(event: dict),三种类型:
    {"type": "tool_call",   "step": i, "name": ..., "args": {...}}
    {"type": "tool_result", "step": i, "name": ..., "result": {...} | "..."}
    {"type": "final",       "content": "..."}
"""
import os
import sys
import uuid

import streamlit as st

# ---- 把项目根目录加入 sys.path,保证可 import harness / attribution ----
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Windows 控制台/输出统一 UTF-8(仅保险,streamlit 一般无需处理)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from harness.loop import run  # noqa: E402

# 同目录的数据集选择器与只读语义层视图(放在 app/ 下,避免继续撑大本文件)。
# 选择驱动重建:选中数据集时由它调 harness.tools.init_engine 把整条链指过去(§3.7)。
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
from dataset_selector import (  # noqa: E402
    render_selector,
    render_semantic_panel,
    selection_error,
    visible_sessions,
)
from attribution_viz import render_from_events  # noqa: E402
from chart_plan import expand_state_key  # noqa: E402
from import_wizard import render_wizard_area  # noqa: E402
from importer_ui import render_importer  # noqa: E402
from session_context import build_context_hint  # noqa: E402

# 展示辅助 / 聊天历史 / 全局样式 / 结论解析,均从 app.py 拆出以压行数;入口只做接线。
from format import _fmt_args, _summarize_result  # noqa: E402
from conclusion import parse_final, render_conclusion, render_token_caption  # noqa: E402
from history import (  # noqa: E402
    _append,
    _configure_history,
    _current_session,
    _init_state,
    _new_session,
    _save_history,
    decode_events,
    encode_events,
)
from theme import _GLOBAL_CSS  # noqa: E402


def _level_orders():
    """语义层的维度层级序(维度名 -> 层级字段元组),供归因树的「层级加深」严格判定;
    取不到时返回 None(可视化模块退化为弱判定)。"""
    try:
        from harness import tools
        return tools.semantic().all_levels()
    except Exception:  # noqa: BLE001  语义层未初始化等:弱判定兜底
        return None


def _render_events_into(ph, events):
    """把已收集的 tool_call / tool_result 事件重绘进占位容器。"""
    lines = []
    for ev in events:
        t = ev.get("type")
        if t == "tool_call":
            lines.append(
                f"🔍 **第 {ev.get('step', '?')} 轮** · 调用工具 `{ev.get('name')}`\n\n"
                f"参数:{_fmt_args(ev.get('args', {})) or '*(无)*'}"
            )
        elif t == "tool_result":
            lines.append(
                f"↩ **第 {ev.get('step', '?')} 轮** · `{ev.get('name')}` 返回:\n\n"
                f"{_summarize_result(ev.get('name'), ev.get('result'))}"
            )
        # final 事件不在此渲染,单独用于结论展示
    ph.markdown("\n\n---\n\n".join(lines), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 侧边栏
# ---------------------------------------------------------------------------
def _render_session_list() -> None:
    """历史会话列表:只列当前数据集的会话(切换数据集不串味)。"""
    sessions = visible_sessions(st.session_state["history"])
    cur = st.session_state.get("current_session")

    # 顶栏:标题 + 新建按钮
    c1, c2 = st.columns([3, 1])
    with c1:
        st.markdown("**历史对话**")
    with c2:
        if st.button("＋", key="new_session_btn", help="新建对话", use_container_width=True):
            _new_session()
            st.rerun()

    if not sessions:
        st.caption("暂无历史记录")
        return
    for sess in sessions:
        is_cur = sess["id"] == cur
        label = ("● " if is_cur else "") + (sess.get("title") or "新对话")
        if st.button(label, key=f"sess_{sess['id']}",
                     use_container_width=True, help=sess.get("created_at", "")):
            st.session_state["current_session"] = sess["id"]
            st.rerun()


def _render_sidebar():
    with st.sidebar:
        # ---- 数据集选择器(选中即重建整条链;只有一个数据集时也照样显示,管路保持通畅)----
        render_selector()
        st.divider()

        # ---- 通用数据导入(CSV/Excel -> 自动识别列名 -> 生成可分析数据集)----
        render_importer()

        # ---- 会话列表(只列当前数据集的会话:历史不串味)----
        _render_session_list()
        st.divider()
        st.markdown("**语义层**")
        with st.expander("📐 查看指标与维度", expanded=False):
            render_semantic_panel()
        st.divider()

        # 当前会话操作
        if st.button("🧹 清空当前对话", use_container_width=True):
            sess = _current_session()
            sess["messages"] = []
            sess["title"] = "新对话"
            _save_history()
            st.rerun()
        if st.button("🗑 删除当前对话", use_container_width=True):
            # 从全量列表里摘掉这一条(其它数据集的会话原样保留)
            cur = st.session_state.get("current_session")
            st.session_state["history"] = [
                s for s in st.session_state["history"] if s["id"] != cur
            ]
            st.session_state["current_session"] = None   # 交由 _current_session 落到本数据集的最新一条
            _save_history()
            st.rerun()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="BI 归因分析 agent", page_icon="📊", layout="wide")
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)
    st.title("📊 BI 归因分析 agent")
    st.caption(
        "别只问数据是多少,问它**为什么** —— 异常检测 → 维度下钻 → 根因定位,"
        "一步步带你找到 GMV 下滑的真凶。"
    )

    _configure_history(os.path.join(ROOT, "chat_history.json"))
    _init_state()
    _render_sidebar()

    # 确认向导进行中:主区域整个让给它(侧边栏仍在,用户能看见自己在哪个数据集上)。
    # 向导是一次性的准入门槛,让位期间不该还能对着半成品数据集提问。
    if render_wizard_area():
        st.stop()

    # 当前会话消息回放:图表从消息里存的**事件流**重建 —— 与当轮走同一段渲染代码,
    # 所以将来再加图型,历史问答也会跟着升级。顺序与当轮保持一致(结论 → 图 → token)。
    sess = _current_session()
    for index, msg in enumerate(sess["messages"]):
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(msg["content"])
            else:
                scope = msg.get("id") or f"{sess['id']}_{index}"   # 老消息没有 id
                parsed = msg.get("parsed")
                render_conclusion(parsed, msg.get("content"))
                # 层级序与图型提示都用**消息里存下来的**:回放要复现当时的画法。
                # 拿「此刻」的语义层会让层级序一改、历史树就静默改观(老消息没存则退回当前值)。
                render_from_events(st, decode_events(msg),
                                   msg.get("level_orders") or _level_orders(), scope=scope,
                                   expanded=st.session_state.get(
                                       expand_state_key(scope), False),
                                   chart_hint=parsed.get("图表")
                                   if isinstance(parsed, dict) else None)
                render_token_caption(msg)

    if not sess["messages"]:
        st.caption("💬 在下方输入你的问题,agent 会先做异常检测、再逐层下钻、最后给出根因结论。")

    prompt = st.chat_input("例如:为什么 2026 年 6 月的 GMV 环比 5 月下滑了?")
    if not prompt:
        return

    # 引擎由侧边栏选择器按当前数据集初始化(选择驱动,§3.7);这里只检查结果:
    # 没指过去就不能分析,否则会拿上一个数据集的引擎给出错误结论。
    if selection_error():
        st.error(f"⚠️ 当前数据集初始化失败,已阻止分析:{selection_error()}")
        return

    with st.chat_message("user"):
        st.markdown(prompt)

    events = []
    usage_info = {}
    msg_id = uuid.uuid4().hex      # 消息 id:切换器的控件 key 拿它做命名空间(回放时唯一)
    with st.chat_message("assistant"):
        with st.status("🔍 正在归因分析…", expanded=True) as status:
            log_ph = st.empty()

            def on_event(ev):
                """回调与 run() 同线程同步调用:把事件收集 + 立即重绘,实现实时刷新。"""
                events.append(ev)
                t = ev.get("type")
                if t == "tool_call":
                    status.update(label=f"🔍 第 {ev.get('step', '?')} 轮 · 正在调用 {ev.get('name')}")
                elif t == "tool_result":
                    status.update(label=f"↩ 第 {ev.get('step', '?')} 轮 · 已收到 {ev.get('name')} 结果")
                elif t == "usage":
                    usage_info.update(ev)
                _render_events_into(log_ph, events)

            try:
                # M9: 会话内多轮记忆 — 从上轮 events 提取摘要注入提示词,
                # 让模型知道"刚才分析到哪了",追问时不重复劳动。
                context_hint = build_context_hint(sess["messages"])
                final = run(prompt, verbose=False, on_event=on_event,
                            context_hint=context_hint)
                status.update(label="✅ 归因分析完成", state="complete", expanded=True)
            except Exception as e:
                status.update(label="⚠️ 分析出错", state="error")
                st.error(f"分析过程出错:{e}")
                return

        parsed, raw = parse_final(final) if final is not None else (None, "")
        render_conclusion(parsed, raw)
        # 下钻过程可视化(§4.6):事件流 -> 归因树 + 各步结果的图(独立成模块,这里只挂接);
        # level_orders 传语义层的层级序(「层级加深」判定走严格版);scope 用消息 id 保证
        # 切换器控件 key 全局唯一;chart_hint 是模型在结论里给的默认图型(可缺省)
        level_orders = _level_orders()     # 与 events 一起存进消息:回放时复现当时的画法
        render_from_events(st, events, level_orders, scope=msg_id,
                           expanded=st.session_state.get(expand_state_key(msg_id), False),
                           chart_hint=parsed.get("图表") if isinstance(parsed, dict) else None)
        if usage_info:
            st.caption(
                f"⚡ 本轮 token 消耗:输入 **{usage_info.get('input_tokens', 0):,}** "
                f"/ 输出 **{usage_info.get('output_tokens', 0):,}**"
                f" · 约 ￥{usage_info.get('cost_cny', 0):.4f}"
            )

        # 写入历史(先 user 后 assistant,保持顺序)
        _append("user", {"content": prompt})
        # events 随消息落盘(= 图表能随聊天记录留存的原因);存紧凑串,见 history.encode_events
        _append("assistant", {"content": raw, "parsed": parsed, "id": msg_id,
                              "events": encode_events(events),
                              "level_orders": level_orders,
                              "input_tokens": usage_info.get("input_tokens"),
                              "output_tokens": usage_info.get("output_tokens"),
                              "cost_cny": usage_info.get("cost_cny")})


if __name__ == "__main__":
    main()
