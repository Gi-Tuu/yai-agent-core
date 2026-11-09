"""子核：一个独立 AgentCore 的联邦视图（Phase 1，进程内）。

子核**不持有父核的任何引用**——这是"子核不能反调父核"的结构性保证，而不是策略约定。
它只暴露两件事：自己的能力清单（manifest）与"按 grant 调用一个小工具"的入口。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shell.federation.grants import CoreGrant, GrantDenied

if TYPE_CHECKING:
    from yai_core import AgentCore


class ChildCore:
    """把一个 AgentCore 包装成"可被父核调用的子核"。

    Args:
        core: 子核自己的 ``AgentCore``（独立工具、策略、记忆、预算）。
        node_id: 图上的节点身份（环检测 / 深度判断用，必须全局稳定）。
        child_id: 能力身份（产品 + 版本），写进 grant，防止跨核串用。
        product: 人类可读的产品名（默认与 child_id 相同）。
    """

    def __init__(
        self,
        core: AgentCore,
        *,
        node_id: str,
        child_id: str = "",
        product: str = "",
    ) -> None:
        node_id = (node_id or "").strip()
        if not node_id:
            raise ValueError("ChildCore.node_id 不能为空")
        self.core = core
        self.node_id = node_id
        self.child_id = (child_id or node_id).strip()
        self.product = (product or self.child_id).strip()

    def manifest(self) -> dict[str, dict[str, Any]]:
        """子核的可调用能力清单（只列有 handler 的工具）。"""
        out: dict[str, dict[str, Any]] = {}
        for spec in self.core.registry.all():
            if spec.handler is None:
                continue
            out[spec.name] = {
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
        return out

    def has_tool(self, name: str) -> bool:
        return self.core.registry.has(name)

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        grant: CoreGrant,
        subject: Any = None,
        call_id: str = "",
    ) -> dict[str, Any]:
        """按 grant 执行一个子核工具；返回结构化结果（不抛子核业务错误）。"""
        grant.check(self.child_id, tool)
        if not self.core.registry.has(tool):
            raise GrantDenied(f"子核 {self.node_id!r} 没有工具 {tool!r}")
        events, ok, text = await self.core.executor.execute(
            tool,
            dict(arguments or {}),
            subject=subject if subject is not None else grant.subject,
            call_id=call_id,
        )
        return {
            "ok": bool(ok),
            "status": "executed" if ok else "child_rejected",
            "child": self.node_id,
            "child_id": self.child_id,
            "tool": tool,
            "result": text,
            "events": len(events),
        }
