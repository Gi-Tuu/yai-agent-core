"""父核侧的有向联邦：把子核工具注册成自己的工具，并守住深度 / 扇出 / 环 / 超时 / 配额。

宿主层实现（内核零改动）。Phase 1 进程内、Phase 2 本机 IPC、Phase 3 增补：
manifest 热更新（``refresh_child``）、每 edge 调用预算与统计（``edge_stats``）、
子核注销（``unregister_child``）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from shell.federation.grants import (
    ChildCallFailed,
    CoreGrant,
    FederationError,
)
from shell.federation.node import ChildCore
from yai_core.tools.hygiene import sanitize_untrusted_text
from yai_core.tools.schema import sanitize_schema
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
        self._children: dict[str, Any] = {}
        self._grants: dict[str, CoreGrant] = {}
        self._exposed: dict[str, set[str]] = {}
        self._grant_all: dict[str, bool] = {}
        self._max_calls: dict[str, int | None] = {}
        self._max_tokens: dict[str, int | None] = {}
        self._stats: dict[str, dict[str, Any]] = {}
        self._semaphore = asyncio.Semaphore(max_fanout)

    # ---------- 注册 / 注销 ----------

    def register_child(
        self,
        child: ChildCore,
        *,
        tools: Iterable[str] | None = None,
        prefix: str | None = None,
        subject: Any = None,
        expires_in: float | None = None,
        max_calls: int | None = None,
        max_tokens: int | None = None,
    ) -> CoreGrant:
        """把子核的**工具子集**注册成父核工具（名字为 ``<prefix>__<tool>``）。

        - ``tools=None`` 表示子核 manifest 全部工具（并允许 ``refresh_child`` 跟随新增）；
        - ``prefix`` 默认取子核产品名，用于避免与父核自有工具重名；
        - ``max_calls`` 是该 edge 的调用次数预算（None = 不限）；
        - 任何一步不满足（重名 / 未知工具 / 空子集）都整体失败，不留半注册状态。
        """
        if not isinstance(child, ChildCore) and not all(
            callable(getattr(child, name, None)) for name in ("manifest", "call")
        ):
            raise TypeError("child 必须是 ChildCore 或同形的 IPC 子核（LinkChild）")
        if max_calls is not None and (
            isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1
        ):
            raise ValueError("max_calls 必须是 >= 1 的整数或 None")
        if max_tokens is not None and (
            isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1
        ):
            raise ValueError("max_tokens 必须是 >= 1 的整数或 None")
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
            specs.append(self._build_spec(prefix, child, tool, grant, manifest[tool]))
        self._children[prefix] = child
        self._grants[prefix] = grant
        self._exposed[prefix] = {f"{prefix}__{tool}" for tool in selected}
        self._grant_all[prefix] = tools is None
        self._max_calls[prefix] = max_calls
        self._max_tokens[prefix] = max_tokens
        self._stats[prefix] = {
            "calls": 0,
            "failures": 0,
            "total_ms": 0.0,
            "tokens": 0,
            "usage": {},
            "last_error": "",
        }
        self.parent.register_tools(specs)
        return grant

    def unregister_child(self, prefix: str) -> bool:
        """注销一个子核：父核侧暴露的工具一并移除。"""
        if prefix not in self._children:
            return False
        for exposed in self._exposed.pop(prefix, set()):
            self.parent.registry.unregister(exposed)
        self._children.pop(prefix, None)
        self._grants.pop(prefix, None)
        self._grant_all.pop(prefix, None)
        self._max_calls.pop(prefix, None)
        self._max_tokens.pop(prefix, None)
        self._stats.pop(prefix, None)
        return True

    def _build_spec(
        self,
        prefix: str,
        child: Any,
        tool: str,
        grant: CoreGrant,
        meta: dict[str, Any],
    ) -> ToolSpec:
        raw_description = f"[子核 {child.node_id}] {meta.get('description', '')}"
        description = sanitize_untrusted_text(raw_description, max_chars=300) or tool
        schema = sanitize_schema(meta.get("input_schema")) or {"type": "object"}
        return ToolSpec(
            name=f"{prefix}__{tool}",
            description=description,
            input_schema=schema,
            handler=self._make_handler(prefix, child, tool, grant),
        )

    def _record_usage(self, prefix: str, usage: Any) -> None:
        """聚合子核回报的 usage；超过该 edge 的 token 预算时失败关闭。"""
        if not isinstance(usage, dict) or not usage:
            return
        stats = self._stats.get(prefix)
        if stats is None:
            return
        totals: dict[str, float] = stats.setdefault("usage", {})
        spent = 0.0
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            totals[key] = float(totals.get(key, 0.0)) + float(value)
        tokens = usage.get("total_tokens", usage.get("tokens"))
        if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
            spent = float(tokens)
        else:
            spent = sum(
                float(value)
                for key, value in usage.items()
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and key.endswith("_tokens")
            )
        stats["tokens"] = int(stats.get("tokens", 0)) + int(spent)
        limit = self._max_tokens.get(prefix)
        if limit is not None and stats["tokens"] > limit:
            raise FederationError(
                f"edge_token_budget_exceeded：子核 {prefix!r} 已用 "
                f"{stats['tokens']} tokens（上限 {limit}）"
            )

    def _make_handler(
        self, prefix: str, child: Any, tool: str, grant: CoreGrant
    ):
        async def handler(**arguments: Any) -> dict[str, Any]:
            return await self._invoke(prefix, child, tool, arguments, grant=grant)

        return handler

    # ---------- manifest 热更新 ----------

    async def refresh_child(self, prefix: str) -> dict[str, Any]:
        """收敛一个子核的当前能力（进程内直接读、IPC 先拉 ``tools.list``）。

        返回 ``{"added": [...], "removed": [...], "manifest_version": "..."}``（子核工具名口径）。
        若注册时用的是 ``tools=None``（全集），新增工具会被自动注册；显式子集保持子集。
        """
        child = self._children.get(prefix)
        grant = self._grants.get(prefix)
        if child is None or grant is None:
            raise FederationError(f"未注册的子核前缀：{prefix!r}")
        refresh = getattr(child, "refresh_manifest", None)
        if callable(refresh):
            await refresh()
        manifest = child.manifest()
        if self._grant_all.get(prefix):
            desired = set(manifest)
        else:
            desired = set(grant.tools) & set(manifest)
        desired_exposed = {f"{prefix}__{tool}" for tool in desired}
        current = self._exposed.get(prefix, set())
        to_remove = current - desired_exposed
        to_add = desired_exposed - current
        for exposed in to_remove:
            self.parent.registry.unregister(exposed)
        specs = [
            self._build_spec(
                prefix,
                child,
                exposed[len(prefix) + 2 :],
                replace(grant, tools=frozenset(desired)),
                manifest[exposed[len(prefix) + 2 :]],
            )
            for exposed in sorted(to_add)
        ]
        if specs:
            self.parent.register_tools(specs)
        self._grants[prefix] = replace(grant, tools=frozenset(desired))
        self._exposed[prefix] = desired_exposed
        version = getattr(child, "manifest_version", None)
        return {
            "added": sorted(name[len(prefix) + 2 :] for name in to_add),
            "removed": sorted(name[len(prefix) + 2 :] for name in to_remove),
            "manifest_version": version() if callable(version) else "",
        }

    async def refresh_all(self) -> dict[str, dict[str, Any]]:
        return {prefix: await self.refresh_child(prefix) for prefix in list(self._children)}

    # ---------- 调用与统计 ----------

    def children(self) -> dict[str, Any]:
        return dict(self._children)

    def grant_for(self, prefix: str) -> CoreGrant | None:
        return self._grants.get(prefix)

    def edge_stats(self, prefix: str) -> dict[str, Any]:
        return dict(self._stats.get(prefix, {}))

    def stats(self) -> dict[str, dict[str, Any]]:
        return {prefix: dict(item) for prefix, item in self._stats.items()}

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
            prefix, child, tool, arguments or {}, grant=grant, subject=subject, call_id=call_id
        )

    async def _invoke(
        self,
        prefix: str,
        child: Any,
        tool: str,
        arguments: dict[str, Any],
        *,
        grant: CoreGrant,
        subject: Any = None,
        call_id: str = "",
    ) -> dict[str, Any]:
        # grant 是注册时签发的身份边界：调用方不能在同一条 grant 上换 subject。
        if subject is None:
            subject = grant.subject
        elif grant.subject is not None and subject != grant.subject:
            raise FederationError(
                "subject 与注册 grant 绑定的主体不一致，已拒绝调用"
            )
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
        limit = self._max_calls.get(prefix)
        stats = self._stats.setdefault(
            prefix,
            {
                "calls": 0,
                "failures": 0,
                "total_ms": 0.0,
                "tokens": 0,
                "usage": {},
                "last_error": "",
            },
        )
        if limit is not None and stats["calls"] >= limit:
            raise FederationError(
                f"edge_budget_exceeded：子核 {prefix!r} 已达 {limit} 次调用上限"
            )
        started = time.perf_counter()
        stats["calls"] += 1
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
                            chain=(*chain, self.node_id),
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
            self._record_usage(prefix, result.get("usage"))
            return result
        except (ChildCallFailed, FederationError) as exc:
            stats["failures"] += 1
            stats["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            stats["total_ms"] += (time.perf_counter() - started) * 1000.0
            _CALL_CHAIN.reset(token)
