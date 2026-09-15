# -*- coding: utf-8 -*-
"""全局视觉样式(BI 分析型产品:克制、专业、高信息密度)。

从 app.py 拆出,只为把入口文件压在 300 行约束内;CSS 本身无依赖,保持原样。
"""

_GLOBAL_CSS = """
<style>
/* ============================================================
   Design Token(参照 UI 设计师 / UX 架构师规范)
   统一色彩 / 字号 / 间距,杜绝散落的硬编码色值
   ============================================================ */
:root {
  /* 色彩 */
  --c-primary: #4F46E5;      /* 主色 indigo */
  --c-primary-soft: #EEF2FF; /* 主色浅底(表头) */
  --c-bg: #FFFFFF;
  --c-bg-subtle: #F8FAFC;    /* 侧边栏 / 次级背景 */
  --c-surface: #F1F5F9;      /* 工具过程日志背景 */
  --c-border: #E2E8F0;
  --c-text: #0F172A;
  --c-text-muted: #64748B;

  /* 字号阶梯:正文 14px 为基准,比 Streamlit 默认(16px)整体收敛一档 */
  --fs-xs: 0.75rem;    /* 12px  caption / 表格 */
  --fs-sm: 0.8125rem;  /* 13px  侧边栏 / 按钮 / 日志 */
  --fs-base: 0.875rem; /* 14px  正文 */
  --fs-lg: 1rem;       /* 16px  h3 */
  --fs-xl: 1.125rem;   /* 18px  h2 */
  --fs-2xl: 1.25rem;   /* 20px  h1 */
}

/* ---------- 全局排版 ---------- */
.stApp {
  font-family: -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC",
               Roboto, Helvetica, Arial, sans-serif;
  color: var(--c-text);
  -webkit-font-smoothing: antialiased;
}
.stMarkdown p, .stMarkdown li {
  font-size: var(--fs-base);
  line-height: 1.65;
}

/* 标题层级:整体下调,主标题不再"巨大" */
h1 { font-size: var(--fs-2xl) !important; font-weight: 700; letter-spacing: -0.01em; }
h2 { font-size: var(--fs-xl) !important; font-weight: 600; }
h3 { font-size: var(--fs-lg) !important; font-weight: 600; }

/* ---------- 侧边栏 ---------- */
section[data-testid="stSidebar"] {
  background-color: var(--c-bg-subtle);
  border-right: 1px solid var(--c-border);
}
section[data-testid="stSidebar"] .stMarkdown p {
  font-size: var(--fs-sm);
  color: var(--c-text-muted);
  margin-bottom: 0.35rem;
}
section[data-testid="stSidebar"] .stButton button {
  font-size: var(--fs-sm);
  padding: 0.28rem 0.6rem;
  min-height: 2rem;
}

/* ---------- 主内容区 ---------- */
.block-container {
  max-width: 960px;
  padding-top: 1.25rem;
  padding-bottom: 3rem;
}

/* ---------- 聊天气泡 ---------- */
/* 用户消息:主色底、白字,右对齐 */
[data-testid="stChatMessage"][aria-label*="user"] {
  background: var(--c-primary);
  border-radius: 10px 10px 3px 10px;
  padding: 0.45rem 0.8rem;
  margin: 0.35rem 0 0.35rem auto;
  max-width: 82%;
}
[data-testid="stChatMessage"][aria-label*="user"] .stMarkdown p {
  color: #FFFFFF;
  font-size: var(--fs-base);
}

/* assistant 消息:白底卡片、细边框,左对齐 */
[data-testid="stChatMessage"][aria-label*="assistant"] {
  background: var(--c-bg);
  border: 1px solid var(--c-border);
  border-radius: 10px 10px 10px 3px;
  padding: 0.55rem 0.9rem;
  margin: 0.35rem auto 0.35rem 0;
  max-width: 94%;
}

/* ---------- 工具过程日志 ---------- */
[data-testid="stStatus"] {
  background: var(--c-surface);
  border-radius: 8px;
}
[data-testid="stStatus"] .stMarkdown p,
[data-testid="stStatus"] .stMarkdown li {
  font-size: var(--fs-sm);
  line-height: 1.5;
}

/* ---------- 提示条(st.success / st.info / st.error)---------- */
[data-testid="stAlert"] {
  font-size: var(--fs-base);
  padding: 0.6rem 0.85rem;
  border-radius: 8px;
}
[data-testid="stAlert"] .stMarkdown p { font-size: var(--fs-base); }

/* ---------- 表格(含 markdown 原生表格与 st.dataframe)---------- */
.stMarkdown table, [data-testid="stTable"], .stDataFrame {
  font-size: var(--fs-xs);
}
.stMarkdown table th, [data-testid="stTable"] th, .stDataFrame th {
  background: var(--c-primary-soft);
  font-weight: 600;
  font-size: var(--fs-sm);
}
.stMarkdown table td, [data-testid="stTable"] td, .stDataFrame td {
  font-size: var(--fs-xs);
}

/* ---------- 折叠面板 ---------- */
[data-testid="stExpander"] { font-size: var(--fs-sm); }
[data-testid="stExpander"] .stMarkdown p,
[data-testid="stExpander"] .stMarkdown li { font-size: var(--fs-xs); }

/* ---------- 行内代码 ---------- */
code {
  font-size: 0.8rem;
  background: var(--c-surface);
  border-radius: 4px;
  padding: 0.1rem 0.3rem;
}
</style>
"""
