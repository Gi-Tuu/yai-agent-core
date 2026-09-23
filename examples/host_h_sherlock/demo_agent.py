"""host_h 的 Core 装配点（CLI 入口）。

宿主就是 sherlock 本身——我们只包了一个 ``lookup_username`` 函数。
这里把 YAI Agent Core 接进来，注册这个函数，模型就能用自然语言调度它。

为了离线、确定、无需 API Key，默认用脚本化模型走"调用查询 → 总结报告"两步；
配置了 ``OPENAI_API_KEY`` 时会自动换成真实模型，同一条链路同样成立。
"""

from __future__ import annotations

from host_h_sherlock import capabilities as cap
from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec

DEFAULT_TASK = "查一下 torvalds 这个用户名在哪些社交平台有账号"


class ScriptedSearchModel:
    """离线确定性模型：调一次 lookup_username，然后给出报告。"""

    def __init__(self, username: str = "torvalds", limit: int = 10) -> None:
        self.calls = 0
        self.username = username
        self.limit = limit

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="h1",
                        name="lookup_username",
                        arguments={"username": self.username, "limit": self.limit},
                    )
                ],
            )
        # 第二次：基于工具返回的结果总结（离线演示里直接给报告模板）。
        return ModelResponse(
            content=(
                f"已用 sherlock 检查 {self.limit} 个社交平台，"
                f"torvalds 命中的站点见上方工具结果；"
                "这就是 YAI 嵌入真实开源项目的效果——宿主没改一行源码，"
                "Core 自动完成了工具发现与调用。"
            )
        )


def build_core(username: str = "torvalds", limit: int = 10) -> AgentCore:
    """装配一个全新的 Core：注册 sherlock 查询工具，默认全放行（离线演示）。"""
    model = ScriptedSearchModel(username, limit)
    core = AgentCore(model, auto_approve_tools=True)
    core.register_tools([build_spec(cap.lookup_username)])
    return core
