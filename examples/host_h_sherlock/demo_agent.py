"""host_h 的 Core 装配点（CLI 与网页共用的唯一 Agent 接线处）。

宿主把 sherlock 包成两个工具：
- ``list_available_sites``：本地元数据，秒回，列出 sherlock 支持的站点；
- ``lookup_username``：真联网查用户名。

脚本化模型演示"多工具调用"：先调 list_available_sites 了解能力边界，
再调 lookup_username 真查 torvalds。这就是"先了解、再行动"的多步工具链。
"""

from __future__ import annotations

from host_h_sherlock import capabilities as cap
from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec

DEFAULT_TASK = "先看看 sherlock 能查哪些站，再查 torvalds 的社交账号"


class ScriptedSearchModel:
    """离线确定性模型：先 list_available_sites，再 lookup_username，最后总结。"""

    def __init__(self, username: str = "torvalds", limit: int = 10) -> None:
        self.calls = 0
        self.username = username
        self.limit = limit

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            # 第一步：先了解有哪些站可查（不联网，快）。
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="h1",
                        name="list_available_sites",
                        arguments={"limit": 10},
                    )
                ],
            )
        if self.calls == 2:
            # 第二步：知道站点范围后，真查 torvalds。
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="h2",
                        name="lookup_username",
                        arguments={"username": self.username, "limit": self.limit},
                    )
                ],
            )
        # 第三步：总结。
        return ModelResponse(
            content=(
                f"先查了 sherlock 支持的站点清单（共数百个），再真查 {self.username}，"
                f"命中站点见上方工具结果。这就是 YAI 的多工具调用：Core 先了解能力、再行动。"
            )
        )


def build_core(username: str = "torvalds", limit: int = 10) -> AgentCore:
    """装配一个全新的 Core：注册 sherlock 两个工具，默认全放行（离线演示）。"""
    model = ScriptedSearchModel(username, limit)
    core = AgentCore(model, auto_approve_tools=True)
    core.register_tools([
        build_spec(cap.list_available_sites),
        build_spec(cap.lookup_username),
    ])
    return core
