"""模型提供者契约：Core 不绑定任何模型厂商。"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from yai_core.types import ModelResponse


@runtime_checkable
class ModelProvider(Protocol):
    """任何实现该协议的对象都可作为 Core 的模型后端。

    - OpenAI 兼容实现见 ``yai_core.llm.openai_compat``
    - 测试/离线演示可使用脚本化假模型

    可选能力标记（类属性，不进协议的强制结构）：

    - ``yai_live_router = True``：真实联网模型后端声明该标记后，
      ``AgentCore(llm_router="auto")`` 才会启用 LLM 路由；
    - 离线/脚本模型不声明（``getattr`` 默认 False），auto 下走规则路由，
      保证离线测试零网络调用、完全确定。禁止用类名字符串嗅探模型类型。
    """

    async def achat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tier: str = "standard",
    ) -> ModelResponse:
        """单轮对话。

        Args:
            messages: OpenAI 线格式消息字典列表（由 Context.llm_messages() 产出）。
            tools: OpenAI 形状的工具描述列表；None 表示本轮不提供工具。
            tier: "standard" / "strong"，供模型路由（便宜模型 vs 强模型）。
        """
        ...
