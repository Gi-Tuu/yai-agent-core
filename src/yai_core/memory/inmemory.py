from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from yai_core.memory.retention import count_cut, ttl_cut, utc_now
from yai_core.types import ChatMessage


class InMemoryStore:
    """默认记忆实现：会话历史 + 简单 KV。v2 再换向量实现。

    v0.3 起构造函数支持历史保留策略（全部 opt-in，默认 None = 永不裁剪，
    行为与旧版逐字节一致）：
    - max_messages：保留最近 N 条消息（按轮对齐，整轮删除，至少留最后一轮）；
    - ttl_seconds：超过 N 秒的历史在写入时与显式 prune() 时清理；
    - clock：可注入时钟（测试用假时钟推进时间，禁止 sleep 等真实时间）。
    ChatMessage 没有时间字段（不改 types.py），时间戳用与 _history 严格
    同长同序的并行列表 _timestamps 保存。
    """

    def __init__(
        self,
        *,
        max_messages: int | None = None,
        ttl_seconds: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._history: list[ChatMessage] = []
        self._timestamps: list[datetime] = []  # 与 _history 严格同长同序
        self._kv: dict[str, str] = {}
        self._kv_expires: dict[str, datetime | None] = {}  # 与 _kv 同键；None=永不过期
        self._max_messages = max_messages
        self._ttl_seconds = ttl_seconds
        self._clock = clock or utc_now

    async def append_history(self, message: ChatMessage) -> None:
        self._history.append(message)
        self._timestamps.append(self._clock())
        # opt-in：未配置任何策略时 prune() 立即返回 0，等价于旧版"只追加"。
        self.prune()

    def history(self) -> list[ChatMessage]:
        return list(self._history)

    def prune(self) -> int:
        """按保留策略裁剪历史前缀，返回删除条数；KV 不动。

        非 MemoryStore 契约方法（与 SqliteStore.close() 同一先例：宿主探测式
        调用，spi/memory.py 的 Protocol 不扩面）。两种策略同时命中时取更大的
        删除上界——两个上界都落在轮首，较大者仍是轮首，工具调用对依然完整。
        """
        roles = [m.role for m in self._history]
        k_count = count_cut(roles, self._max_messages)
        k_ttl = None
        if self._ttl_seconds is not None:
            cutoff = self._clock() - timedelta(seconds=self._ttl_seconds)
            # 严格 <：时间戳恰好等于 cutoff 的消息保留（边界语义钉在测试里）。
            expired = [ts < cutoff for ts in self._timestamps]
            k_ttl = ttl_cut(roles, expired)
        cuts = [k for k in (k_count, k_ttl) if k is not None]
        if not cuts:
            return 0
        k = max(cuts)
        del self._history[:k]
        del self._timestamps[:k]
        return k

    async def put(
        self, key: str, value: str, ttl_seconds: int | None = None
    ) -> None:
        """写 KV；ttl_seconds 非 None 时记录过期时刻，get 时惰性过期。"""
        self._kv[key] = value
        self._kv_expires[key] = (
            self._clock() + timedelta(seconds=ttl_seconds)
            if ttl_seconds is not None
            else None
        )

    async def get(self, key: str) -> str | None:
        """读 KV；当前时间严格超过过期时刻则删除并返回 None（等于过期时刻保留）。"""
        expires = self._kv_expires.get(key)
        if expires is not None and self._clock() > expires:
            self._kv.pop(key, None)
            self._kv_expires.pop(key, None)
            return None
        return self._kv.get(key)

    async def clear(self) -> None:
        self._history.clear()
        self._timestamps.clear()
        self._kv.clear()
        self._kv_expires.clear()
