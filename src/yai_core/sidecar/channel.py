"""Sidecar 的 Channel 实现：把审批/澄清转发给宿主，而不是本地终端。

事件本身不走这里（``AgentCore.astream`` 是唯一出口，server 直接转发）；
这里只处理两个必须等宿主回答的交互：

- ``approval/request``：宿主 UI 决定是否放行；
- ``clarify/request``：宿主向用户反问，返回补充文本。

超时一律按“拒绝/空回答”处理，避免宿主失联后任务永久挂起。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

#: 宿主对一次审批/澄清的最长等待（秒）；超时按拒绝/空回答处理。
DEFAULT_INTERACTION_TIMEOUT = 600.0

RequestHost = Callable[[str, dict[str, Any]], Awaitable[Any]]


class SidecarChannel:
    """Channel SPI 的 sidecar 实现。"""

    #: 审批上下文（preview / risk / scope / target）一并转给宿主 UI。
    yai_approval_context_aware = True

    def __init__(
        self,
        request_host: RequestHost,
        *,
        timeout: float = DEFAULT_INTERACTION_TIMEOUT,
    ) -> None:
        self._request_host = request_host
        self.timeout = timeout

    async def emit(self, event: Any) -> None:  # noqa: ANN401 - 事件由 astream 统一发
        return None

    async def confirm(
        self,
        tool_name: str,
        arguments: dict,
        context: dict | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"tool": tool_name, "arguments": arguments}
        if context:
            payload["context"] = context
        try:
            async with asyncio.timeout(self.timeout):
                result = await self._request_host("approval/request", payload)
        except TimeoutError:
            return False
        if isinstance(result, bool):
            return result
        if isinstance(result, dict):
            return bool(result.get("approved"))
        return False

    async def ask(self, question: str) -> str:
        try:
            async with asyncio.timeout(self.timeout):
                result = await self._request_host(
                    "clarify/request", {"question": question}
                )
        except TimeoutError:
            return ""
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            return str(result.get("answer", ""))
        return str(result)
