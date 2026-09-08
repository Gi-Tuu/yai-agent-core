from __future__ import annotations

from yai_core.types import ChatMessage


class InMemoryStore:
    """默认记忆实现：会话历史 + 简单 KV。v2 再换 SQLite/向量实现。"""

    def __init__(self) -> None:
        self._history: list[ChatMessage] = []
        self._kv: dict[str, str] = {}

    async def append_history(self, message: ChatMessage) -> None:
        self._history.append(message)

    def history(self) -> list[ChatMessage]:
        return list(self._history)

    async def put(self, key: str, value: str) -> None:
        self._kv[key] = value

    async def get(self, key: str) -> str | None:
        return self._kv.get(key)

    async def clear(self) -> None:
        self._history.clear()
        self._kv.clear()
