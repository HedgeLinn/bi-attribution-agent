# -*- coding: utf-8 -*-
"""最终结论的解析与展示。

从 app.py 拆出,只为把入口文件压在 300 行约束内。模型最终返回的字符串可能带
JSON 围栏、可能带前置说明文字,parse_final 尽力提取 dict;失败时原样展示。
"""

import json

import streamlit as st


def _strip_fences(s):
    """去掉模型可能包在 JSON 外的 ``` 围栏。"""
    s = s.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def parse_final(content):
    """尝试把最终结论字符串解析成 dict;失败返回 (None, 原文)。"""
    if not content:
        return None, content or ""
    s = _strip_fences(content)
    # 1) 整串直接解析
    for candidate in (s, content):
        try:
            obj = json.loads(candidate)
            return obj, content
        except Exception:
            pass
    # 2) 找首个 { 到最后一个 } 之间的子串再试(模型可能带了前置说明文字)
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(s[start:end + 1]), content
        except Exception:
            pass
    return None, content


def render_conclusion(parsed, raw):
    """解析出 JSON 则分块展示;否则原文展示。"""
    if isinstance(parsed, dict):
        if "结论" in parsed:
            st.success(f"**结论**:{parsed['结论']}")
        chain = parsed.get("证据链") or parsed.get("证据") or parsed.get("evidence")
        if chain is not None:
            st.markdown("**证据链**")
            if isinstance(chain, list):
                for it in chain:
                    st.markdown(f"- {it}")
            else:
                st.markdown(f"- {chain}")
        if "建议" in parsed:
            st.info(f"💡 **建议**:{parsed['建议']}")
        shown = {"结论", "证据链", "证据", "evidence", "建议"}
        extra = {k: v for k, v in parsed.items() if k not in shown}
        if extra:
            with st.expander("其它字段"):
                st.json(extra)
    else:
        st.markdown(raw or "*(无内容)*")


def render_assistant_msg(msg):
    """渲染一条 assistant 消息:结论 + 可选 token 消耗说明。"""
    render_conclusion(msg.get("parsed"), msg.get("content"))
    if msg.get("input_tokens") is not None:
        st.caption(
            f"⚡ 本轮 token 消耗:输入 **{msg.get('input_tokens', 0):,}** "
            f"/ 输出 **{msg.get('output_tokens', 0):,}**"
            f" · 约 ￥{msg.get('cost_cny', 0):.4f}"
        )
