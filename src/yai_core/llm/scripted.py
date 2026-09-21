"""离线脚本化模型：零依赖、确定性，用于快速验证嵌入与离线测试。

这是 :class:`yai_core.spi.model.ModelProvider` 契约的最简实现，**不发起任何网络请求、
不需要 API Key**。它让宿主在接入真实模型之前，就能用 ``pip install yai-agent-core``
（零第三方依赖）在 30 秒内验证内核已正确嵌入、Agent Loop 能发现并执行工具。

典型用途：

- 首次接入时的零成本冒烟（不必先申请模型 Key）；
- 离线单元测试 / CI（结果完全确定）；
- 教学演示：看清"模型决定调什么工具，内核负责执行"的分工。

真实上线时把它整体替换为 :class:`yai_core.llm.openai_compat.OpenAICompatProvider`
（``[llm]`` 可选依赖）即可，其余代码不变。
"""

from __future__ import annotations

from typing import Any

from yai_core.types import ModelResponse


class ScriptedModel:
    """按预设顺序返回响应的确定性模型。

    注意：本类**不声明** ``yai_live_router`` 类属性，因此在
    ``AgentCore(llm_router="auto")`` 下会走零成本规则路由，保证离线零网络、
    结果可复现。

    用法::

        model = ScriptedModel([
            ModelResponse(content="", tool_calls=[
                ToolCallRequest(id="c1", name="search_notes",
                                arguments={"keyword": "周报"}),
            ]),
            ModelResponse(content="本周周报包含 2 条记录。"),
        ])
    """

    def __init__(self, responses: list[ModelResponse]) -> None:
        if not responses:
            raise ValueError("ScriptedModel 至少需要一个 ModelResponse")
        self._responses = list(responses)
        # 实际调用次数（可在测试中断言 Agent Loop 调用了几轮模型）。
        self.calls = 0

    async def achat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tier: str = "standard",
    ) -> ModelResponse:
        # 按顺序返回；调用次数超过预设长度后重复最后一条，避免下标越界。
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp
