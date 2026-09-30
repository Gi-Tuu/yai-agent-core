"""Channel SPI 的通用网页实现（浮窗与内核之间的交互通道）。

- :meth:`emit` 不做事：事件统一由 :class:`shell.session.ShellSession` 经 SSE 推送，
  避免同一条事件被发送两次；
- :meth:`ask` / :meth:`confirm` 挂起当前内核运行，等待浏览器经
  ``/api/answer``（澄清回答）或 ``/api/decision``（授权）回传；
- 超时按"空回答 / 拒绝"收尾，保证任务不会永久挂起。

放入 SSE 的事件类型一律使用内核 :class:`yai_core.types.EventType` 标准名。
"""

from __future__ import annotations

import asyncio
from typing import Any

#: 等待浏览器授权/澄清的最长秒数；超时按拒绝/空回答处理。
PROMPT_TIMEOUT_SECONDS = 600


class WebChannel:
    """网页 Channel：ask/confirm 异步等待浏览器决策。"""

    def __init__(self, run: dict, loop: asyncio.AbstractEventLoop) -> None:
        self._run = run
        self._loop = loop

    async def emit(self, event: Any) -> None:
        # 事件由 ShellSession 直接从 core.astream 读取并推送，这里不重复处理。
        return None

    async def ask(self, question: str) -> str:
        return await self._prompt(
            "ask", {"question": question}, ""
        )

    async def confirm(self, tool_name: str, arguments: dict) -> bool:
        payload = {"tool": tool_name, "arguments": arguments}
        return bool(await self._prompt("confirm", payload, False))

    async def _prompt(
        self, kind: str, data: dict, default: Any
    ) -> Any:
        future = self._loop.create_future()
        self._run["pending"] = (kind, future)
        # 使用内核 EventType 标准名，浮窗按同一套映射渲染。
        event_type = "clarify_requested" if kind == "ask" else "permission_asked"
        self._run["queue"].put({"type": event_type, "data": data})
        try:
            return await asyncio.wait_for(
                future, timeout=PROMPT_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return default
        finally:
            self._run["pending"] = None
