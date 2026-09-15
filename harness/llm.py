"""模型接入层:双协议支持(Anthropic / OpenAI),按环境变量自动分派。

这是 harness 的 Model 组件。所有 LLM 调用都经这里,不散落在别处。

协议分派规则:
  - 设置了 `ANTHROPIC_AUTH_TOKEN` -> Anthropic 协议(ChatAnthropic)
    端点取 `ANTHROPIC_BASE_URL`(阿里云百炼 token-plan 应用端点),
    默认模型 `deepseek-v4-pro-0813`。
  - 否则 -> OpenAI 协议(ChatOpenAI,原路径保持不动)
    端点取 `BI_API_BASE`(默认 http://new-api.mypy.cn/v1),
    模型 `deepseek-v4-flash`。

两种协议都把 llm_cost 记账回调挂进 callbacks(可选依赖,未装退化为 no-op)。
"""
import os
import sys

from langchain_openai import ChatOpenAI

# 挂全局 llm_cost 记账回调(可选依赖)。本机装了 llm_cost 技能才启用;
# 其它机器 clone 后没有该技能,则退化为 no-op,不影响主流程。
try:
    sys.path.insert(0, "C:/Users/hzl/.claude/skills/llm_cost")
    from llm_cost import get_cost_callback  # noqa: E402
except Exception:  # noqa: BLE001
    def get_cost_callback():
        """llm_cost 不可用时的空回调。"""
        return []

# ---- 双协议配置(模块加载时求值一次;环境变量在进程启动前就定好了) ----
_ANTHROPIC_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
_ANTHROPIC_BASE = os.environ.get("ANTHROPIC_BASE_URL", "")
_OPENAI_BASE = os.environ.get("BI_API_BASE", "http://new-api.mypy.cn/v1")
_OPENAI_KEY = os.environ.get("BI_API_KEY", "")

PROVIDER = "anthropic" if _ANTHROPIC_TOKEN else "openai"
MODEL = "deepseek-v4-pro-0813" if PROVIDER == "anthropic" else "deepseek-v4-flash"

# 两个协议同一语义:deepseek 系列默认开启思考,显式关闭——加快响应、便于 tool calling,
# 也让 AIMessage.content 保持纯 str(Anthropic 协议下开思考会返回 thinking 块,content 变 list)
EXTRA_BODY = {"thinking": {"type": "disabled"}}
# Anthropic 协议:max_tokens 必填;归因结论较长(最多 20 轮下钻),给足输出预算
MAX_TOKENS = int(os.environ.get("BI_MAX_TOKENS", "8192"))


def _anthropic_chat(temperature: float):
    """构建 ChatAnthropic。懒加载 langchain_anthropic:只用 OpenAI 协议的环境
    不需要装这个包(比如跑离线测试的机器),import 失败只在真正走这条路径时发生。"""
    from langchain_anthropic import ChatAnthropic  # noqa: E402

    kwargs = {
        "model": MODEL,
        "api_key": _ANTHROPIC_TOKEN,        # Anthropic SDK 会将 api_key 作为 x-api-key 发送
        "temperature": temperature,
        "max_tokens": MAX_TOKENS,
        "extra_body": EXTRA_BODY,           # 关思考:网关按厂商扩展字段透传(已实测生效)
        "callbacks": [get_cost_callback()],
    }
    if _ANTHROPIC_BASE:
        kwargs["base_url"] = _ANTHROPIC_BASE
    return ChatAnthropic(**kwargs)


def build_llm(tools=None, temperature: float = 0.0):
    """构建一个挂好记账回调的 chat 模型实例,按环境变量分派协议。

    温度设 0,让归因决策稳定可复现(分析任务不需要发散)。
    tools 是 LangChain 工具列表,两个协议都通过 bind_tools 绑定
    (LangChain 负责把工具定义翻译成各协议原生的 tool 格式)。
    """
    if PROVIDER == "anthropic":
        llm = _anthropic_chat(temperature)
    else:
        llm = ChatOpenAI(
            base_url=_OPENAI_BASE,
            api_key=_OPENAI_KEY,
            model=MODEL,
            temperature=temperature,
            extra_body=EXTRA_BODY,
            callbacks=[get_cost_callback()],
        )
    if tools:
        llm = llm.bind_tools(tools)
    return llm