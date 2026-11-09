"""父核侧的有向联邦：把子核工具注册成自己的工具，并守住深度 / 扇出 / 环 / 超时。

Phase 1 是**进程内**实现：没有网络、没有发现文件、grant 是内存对象。
协议与跨进程形态见 `docs/guides/federation.md`。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from shell.federation.grants import (
    ChildCallFailed,
    CoreGrant,
    FederationError,
)
from shell.federation.node import ChildCore
from yai_core.types import ToolSpec

if TYPE_CHECKING:
    from yai_core import AgentCore

#: 当前调用链（节点 id 序列），跨 Federation 实例共享，用于深度与环检测。
_CALL_CHAIN: ContextVar[tuple[str, ...]] = ContextVar("yai_federation_call_chain", default=())


class Federation:
    """父核侧的联邦注册表。

    Args:
        parent: 父核自己的 ``AgentCore``。
        node_id: 父核在图上的节点身份。
        max_depth: 最大调用深度（父核直接调用算 1 层）。
        max_fanout: 同一父核同时进行的子核调用上限。
        call_timeout: 单次子核调用的墙钟超时（秒）。
    """

    def __init__(
        self,
        parent: AgentCore,
        *,
        node_id: str = "root",
        max_depth: int = 3,
        max_fanout: int = 16,
        call_timeout: float = 30.0,
    ) -> None:
        for label, value, minimum in (
            ("max_depth", max_depth, 1),
            ("max_fanout", max_fanout, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"Federation.{label} 必须是 >= {minimum} 的整数")
        if isinstance(call_timeout, bool) or not isinstance(call_timeout, (int, float)):
            raise ValueError("Federation.call_timeout 必须是数字")
        if call_timeout <= 0:
            raise ValueError("Federation.call_timeout 必须为正数")
        self.parent = parent
        self.node_id = (node_id or "").strip()
        if not self.node_id:
            raise ValueError("Federation.node_id 不能为空")
        self.max_depth = max_depth
        self.max_fanout = max_fanout
        self.call_timeout = float(call_timeout)
        self._children: dict[str, ChildCore] = {}
        self._grants: dict[str, CoreGrant] = {}
        self._semaphore = asyncio.Semaphore(max_fanout)

    # ---------- 注册 ----------

    def register_child(
        self,
        child: ChildCore,
        *,
        tools: Iterable[str] | None = None,
        prefix: str | None = None,
        subject: Any = None,
        expires_in: float | None = None,
    ) -> CoreGrant:
        """把子核的**工具子集**注册成父核工具（名字为 ``<prefix>__<tool>``）。

        - ``tools=None`` 表示子核 manifest 全部工具；
        - ``prefix`` 默认取子核产品名，用于避免与父核自有工具重名；
        - 任何一步不满足（重名 / 未知工具 / 空子集）都整体失败，不留半注册状态。
        """
        if not isinstance(child, ChildCore):
            raise TypeError("child 必须是 ChildCore 实例")
        prefix = (prefix or child.product or child.node_id).strip()
        if not prefix:
            raise ValueError("子核前缀不能为空")
        if prefix in self._children:
            raise ValueError(f"子核前缀 {prefix!r} 已注册")
        manifest = child.manifest()
        if not manifest:
            raise ValueError(f"子核 {child.node_id!r} 没有可调用工具")
        selected = list(tools) if tools is not None else list(manifest)
        if not selected:
            raise ValueError("tools 不能为空列表（要全部可传 None）")
        unknown = [name for name in selected if name not in manifest]
        if unknown:
            raise ValueError(f"子核 {child.node_id!r} 没有这些工具：{unknown}")
        grant = CoreGrant(
            edge=f"{self.node_id}->{child.node_id}",
            child_id=child.child_id,
            tools=frozenset(selected),
            subject=subject,
            expires_at=(
                None if expires_in is None else time.monotonic() + float(expires_in)
            ),
        )
        specs: list[ToolSpec] = []
        for tool in selected:
            exposed = f"{prefix}__{tool}"
            if self.parent.registry.has(exposed):
                raise ValueError(f"父核已有同名工具 {exposed!r}，请换一个 prefix")
            meta = manifest[tool]
            specs.append(
                ToolSpec(
                    name=exposed,
                    description=f"[子核 {child.node_id}] {meta['description']}",
                    input_schema=meta["input_schema"],
                    handler=self._make_handler(child, tool, grant),
                )
            )
        self._children[prefix] = child
        self._grants[prefix] = grant
        self.parent.register_tools(specs)
        return grant

    def _make_handler(self, child: ChildCore, tool: str, grant: CoreGrant):
        async def handler(**arguments: Any) -> dict[str, Any]:
            return await self._invoke(child, tool, arguments, grant=grant)

        return handler

    # ---------- 调用 ----------

    def children(self) -> dict[str, ChildCore]:
        return dict(self._children)

    def grant_for(self, prefix: str) -> CoreGrant | None:
        return self._grants.get(prefix)

    async def invoke(
        self,
        prefix: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        subject: Any = None,
        call_id: str = "",
    ) -> dict[str, Any]:
        """宿主代码直接调用子核工具（不经模型），语义与模型调用一致。"""
        child = self._children.get(prefix)
        grant = self._grants.get(prefix)
        if child is None or grant is None:
            raise FederationError(f"未注册的子核前缀：{prefix!r}")
        return await self._invoke(
            child, tool, arguments or {}, grant=grant, subject=subject, call_id=call_id
        )

    async def _invoke(
        self,
        child: ChildCore,
        tool: str,
        arguments: dict[str, Any],
        *,
        grant: CoreGrant,
        subject: Any = None,
        call_id: str = "",
    ) -> dict[str, Any]:
        chain = _CALL_CHAIN.get()
        if child.node_id in chain:
            raise FederationError(
                "检测到循环调用：" + " -> ".join((*chain, child.node_id))
            )
        depth = len(chain) + 1
        if depth > self.max_depth:
            raise FederationError(
                f"超过最大委派深度 {self.max_depth}（当前链：{' -> '.join(chain) or '-'}）"
            )
        token = _CALL_CHAIN.set((*chain, self.node_id))
        try:
            async with self._semaphore:
                try:
                    async with asyncio.timeout(self.call_timeout):
                        result = await child.call(
                            tool,
                            arguments,
                            grant=grant,
                            subject=subject,
                            call_id=call_id,
                        )
                except TimeoutError:
                    raise ChildCallFailed(
                        f"子核 {child.node_id!r} 调用超时（{self.call_timeout:g}s）"
                    ) from None
            if not result.get("ok"):
                detail = str(result.get("result") or "").strip()[:500]
                raise ChildCallFailed(
                    f"子核 {child.node_id!r} 未完成 {tool!r}：{detail or '无详情'}"
                )
            return result
        finally:
            _CALL_CHAIN.reset(token)
