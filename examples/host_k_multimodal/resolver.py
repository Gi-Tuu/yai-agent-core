"""宿主侧资源引用解析器：多模态消息里"资源引用"到底该由谁解析。

这一段是**样板**（示例宿主的一部分，不是内核的一部分）。它演示四件事：

1. **内核不解析外部资源**：模型给的只是"句柄"（如 ``res://note/1``），内核既不抓取
   也不解析——解析发生在宿主这一侧，因为**授权与生命周期都归宿主**。
2. **作用域由宿主判定**：每条资源记录带一个 ``scope``；调用者只在自己被授权的
   scope 里才拿得到。模型无法通过参数扩大自己的作用域（它只能选句柄）。
3. **有效期由宿主判定**：记录带 ``expires_at``，过期即拒；内核没有"时钟真相"。
4. **路径仍走内核那套纯函数**：``normalize_relative`` + ``resolve_within_root``——
   和文件能力后端**同一套语义**，避免"闸门认的路径"与"实际读的路径"不是一回事。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from yai_core.spi.resources import (
    ResourceError,
    ResourcePolicy,
    normalize_relative,
    resolve_within_root,
)


class ResourceResolutionError(ValueError):
    """引用解析失败的基类（宿主可据此回灌给模型）。"""


class UnknownResourceError(ResourceResolutionError):
    """模型给了宿主不认识的句柄。"""


class ResourceScopeError(ResourceResolutionError):
    """调用者不在该资源的授权作用域内。"""


class ResourceExpiredError(ResourceResolutionError):
    """引用已过期。"""


class ResourceTooLargeError(ResourceResolutionError):
    """资源超过宿主的读取上限。"""


@dataclass(frozen=True)
class HostResource:
    """宿主登记的一条资源：句柄 → root 下的相对路径 + 作用域 + 有效期。

    ``scope`` 留空表示"不做额外作用域限制"（仍受 root 约束与过期检查）。
    """

    handle: str
    relative_path: str
    scope: str = ""
    expires_at: str = ""


@dataclass
class HostResourceResolver:
    """宿主侧解析器：句柄 → 文本。

    关键口径：**模型只能选句柄，判权与判有效期的是宿主**。
    """

    root: Path
    resources: Mapping[str, HostResource]
    max_bytes: int = 200_000
    policy: ResourcePolicy = ResourcePolicy()
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def resolve(self, handle: str, *, allowed_scopes: Iterable[str]) -> str:
        """解析一个句柄并返回文本；任何不满足的条件都抛 :class:`ResourceResolutionError`。"""
        record = self.resources.get(handle)
        if record is None:
            raise UnknownResourceError(f"未知资源句柄：{handle!r}")
        self._check_scope(record, allowed_scopes)
        self._check_expiry(record)
        try:
            target = resolve_within_root(self.root, record.relative_path, policy=self.policy)
        except ResourceError as exc:
            # 路径越界等内核纯函数错误，统一包装成宿主自己的错误语义：
            # 对模型只回一种错误（"这个引用拿不到"），不泄漏路径细节。
            raise ResourceResolutionError(f"资源 {handle} 不可访问") from exc
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise ResourceResolutionError(f"资源不可读：{handle}") from exc
        if size > self.max_bytes:
            raise ResourceTooLargeError(
                f"资源 {handle} 约 {size} 字节，超过宿主上限 {self.max_bytes} 字节"
            )
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ResourceResolutionError(f"资源不可读：{handle}") from exc

    def _check_scope(self, record: HostResource, allowed_scopes: Iterable[str]) -> None:
        if not record.scope:
            return
        allowed = set(allowed_scopes)
        if record.scope not in allowed:
            raise ResourceScopeError(
                f"资源 {record.handle} 需要作用域 {record.scope!r}，当前调用者没有"
            )

    def _check_expiry(self, record: HostResource) -> None:
        if not record.expires_at:
            return
        try:
            expires = datetime.fromisoformat(record.expires_at)
        except ValueError as exc:
            raise ResourceResolutionError(
                f"资源 {record.handle} 的有效期写法无法解析：{record.expires_at!r}"
            ) from exc
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if self.clock() >= expires:
            raise ResourceExpiredError(f"资源 {record.handle} 已于 {record.expires_at} 失效")


def relative_handle(path: str) -> str:
    """把宿主内部的相对路径转成句柄（示例约定：``res://<相对路径>``）。"""
    return f"res://{normalize_relative(path)}"
