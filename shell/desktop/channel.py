"""Channel SPI 的 Qt 原生实现：界面就是通道，不再有本地 HTTP 回环。

- :meth:`emit` 不做事：事件统一由 :class:`shell.desktop.specialist.SpecialistRuntime`
  从内核事件流逐条投递给工作台，避免同一条事件发送两次；
- :meth:`ask` / :meth:`confirm` 挂起当前内核运行，直到用户在原生面板上
  点授权或提交澄清回答；
- 超时按"空回答 / 拒绝"收尾，保证任务不会永久挂起。

界面看到的事件类型一律使用内核 :class:`yai_core.types.EventType` 标准名。
"""

from __future__ import annotations

import asyncio
from typing import Any

#: 等待用户授权/澄清回答的最长秒数；超时按拒绝 / 空回答处理。
PROMPT_TIMEOUT_SECONDS = 600


class QtChannel:
    """Qt 通道：ask/confirm 异步等待原生界面决策。"""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    @property
    def permission_mode(self) -> str:
        """该专员当前的权限挡位：真 Core 装配策略时按此决定。"""
        return self._runtime.permission_mode

    async def emit(self, event: Any) -> None:
        # 事件由运行时从 astream 统一投递，这里不重复发送。
        return None

    async def ask(self, question: str) -> str:
        answer = await self._prompt("ask", {"question": question}, "")
        return str(answer)

    async def confirm(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        payload = {"tool": tool_name, "arguments": arguments}
        return bool(await self._prompt("confirm", payload, False))

    async def _prompt(self, kind: str, data: dict, default: Any) -> Any:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._runtime.open_prompt(kind, future, data)
        try:
            return await asyncio.wait_for(future, PROMPT_TIMEOUT_SECONDS)
        except TimeoutError:
            return default
        finally:
            self._runtime.close_prompt()
