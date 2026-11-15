"""幂等结果缓存与权限依赖快照。

从 ``tools/executor.py`` 拆出；执行器持有 :class:`IdempotencyCache` 实例。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from yai_core.types import ReceiptStatus


@dataclass(frozen=True)
class _PermissionDependency:
    """仅存于进程内缓存的权限依据，不进入事件、收据或模型正文。"""

    name: str
    spec: Any
    arguments: dict[str, Any] | None


class IdempotencyCache:
    """按 (tool, key) 缓存结果与权限依赖；参数指纹不一致时不命中。"""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._entries: OrderedDict[
            tuple[str, str],
            tuple[bool, str, ReceiptStatus, str, str, tuple[_PermissionDependency, ...]],
        ] = OrderedDict()

    def get(
        self, tool: str, key: str, fingerprint: str
    ) -> tuple[
        tuple[bool, str, ReceiptStatus, str, tuple[_PermissionDependency, ...]], bool
    ] | None:
        """返回 (缓存结果及权限依赖, 参数是否与首次执行一致)。"""
        if not key:
            return None
        cached = self._entries.pop((tool, key), None)
        if cached is None:
            return None
        self._entries[(tool, key)] = cached
        ok, text, status, error, cached_fingerprint, dependencies = cached
        return (ok, text, status, error, dependencies), cached_fingerprint == fingerprint

    def put(
        self,
        tool: str,
        key: str,
        fingerprint: str,
        *,
        ok: bool,
        text: str,
        status: ReceiptStatus,
        error: str = "",
        dependencies: list[_PermissionDependency],
    ) -> None:
        if not key or status == "not_started":
            return
        self._entries.pop((tool, key), None)
        self._entries[(tool, key)] = (
            ok, text, status, error, fingerprint, tuple(dependencies)
        )
        while len(self._entries) > self._limit:
            self._entries.popitem(last=False)


