"""父子核的调用凭证与错误类型（Phase 1）。

Phase 1 用**进程内不可变对象**表达授权：父核持有 `CoreGrant`，子核在每次调用前校验。
Phase 2（本机 IPC）会把它换成 HMAC 签名串，字段与语义保持不变。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


class FederationError(RuntimeError):
    """联邦层错误基类。"""


class GrantDenied(FederationError):
    """grant 不存在 / 已过期 / 未授予该工具 / 不属于本子核。"""


class ChildCallFailed(FederationError):
    """子核调用失败（子核策略拒绝、工具报错、超时等），以工具错误回流父核模型。"""


class ChildUnavailable(FederationError):
    """子核不可用（Phase 2 IPC 语义；Phase 1 仅在做生命周期检查时使用）。"""


@dataclass(frozen=True)
class CoreGrant:
    """一条父 → 子边上的调用授权。

    Args:
        edge: 边的标识，形如 ``parent-node->child-node``。
        child_id: 子核身份（产品 + 核版本），用于防止 grant 串核。
        tools: 允许调用的**子核工具名**（不是父核暴露后的前缀名）。
        subject: 这次授权代表的用户主体；调用时透传给子核执行器。
        expires_at: ``time.monotonic()`` 口径的过期时间戳；None 表示不过期。
    """

    edge: str
    child_id: str
    tools: frozenset[str]
    subject: Any = None
    expires_at: float | None = None

    def is_expired(self, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (time.monotonic() if now is None else now) >= self.expires_at

    def check(self, child_id: str, tool: str) -> None:
        """校验这次调用是否在授权范围内；不通过一律抛 ``GrantDenied``（失败关闭）。"""
        if self.child_id != child_id:
            raise GrantDenied(f"grant 属于子核 {self.child_id!r}，不能用于 {child_id!r}")
        if self.is_expired():
            raise GrantDenied(f"grant 已过期（edge={self.edge}）")
        if tool not in self.tools:
            raise GrantDenied(f"工具 {tool!r} 不在 grant 子集内（edge={self.edge}）")
