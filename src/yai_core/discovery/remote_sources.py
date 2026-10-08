"""v0.8 远程动态发现源：MCP 目录与 OpenAPI 服务目录实现同一 ``ToolDiscovery``。

复用 integrations 里的 :class:`McpToolBridge` / :class:`OpenApiToolBridge`，把"一组外部
能力源"包装成内核可注入的发现源：

- **懒连接**：首次 ``discover`` 时才连接所有源；单个源失败 / 超时被跳过，不拖垮其余；
- **连接常驻**：bridge 的 handler 依赖底层 client，发现源保持连接并缓存 specs，不提前关闭；
- **去重**：跳过已注册工具（available）与同批重名；
- **停机**：``aclose()`` 关闭所有 bridge。

配置即白名单：宿主显式配置了哪些外部源，它们的工具才"可被发现"；``need`` 当前不做
自由文本相关性筛选（远程工具名与自由文本匹配不可靠，易漏），保留给将来语义检索层。
注册去重由内核、执行授权由 PermissionPolicy 分别负责，发现源不越权。内核发现闭环零改动。
"""

from __future__ import annotations

from typing import Any, Protocol

from yai_core.types import ToolSpec


class _RemoteBridge(Protocol):
    """MCP / OpenAPI bridge 的最小结构（duck typing，测试可直接注入假对象）。"""

    async def connect(self) -> list[ToolSpec]: ...

    async def aclose(self) -> None: ...


class RemoteSourceDiscovery:
    """一组远程 bridge（MCP / OpenAPI）的懒连接发现源，实现 ToolDiscovery。"""

    def __init__(self, bridges: list[Any]) -> None:
        self._bridges = list(bridges)
        self._cache: list[ToolSpec] | None = None

    async def _ensure_specs(self) -> list[ToolSpec]:
        """懒连接并缓存：只在首次调用时连接各源；单源失败跳过。"""
        if self._cache is None:
            specs: list[ToolSpec] = []
            for bridge in self._bridges:
                try:
                    specs.extend(await bridge.connect())
                except Exception:  # noqa: BLE001 - 外部源不可用不应阻断发现闭环
                    continue
            self._cache = specs
        return self._cache

    async def discover(
        self,
        need: str,
        *,
        task: str,
        available: list[str],
    ) -> list[ToolSpec]:
        specs = await self._ensure_specs()
        already = set(available)
        seen: set[str] = set()
        out: list[ToolSpec] = []
        for spec in specs:
            if spec.name in already or spec.name in seen:
                continue
            seen.add(spec.name)
            out.append(spec)
        return out

    async def aclose(self) -> None:
        """停机钩子：关闭所有 bridge；单个关闭失败不影响其余。"""
        for bridge in self._bridges:
            try:
                await bridge.aclose()
            except Exception:  # noqa: BLE001
                continue


def mcp_discovery(configs: list[Any]) -> RemoteSourceDiscovery:
    """从一组 MCP Server 配置构造发现源（不触发连接，第三方 mcp 包在 connect 时才需要）。"""
    from yai_core.integrations.mcp.client import McpToolBridge

    return RemoteSourceDiscovery([McpToolBridge(c) for c in configs])


def openapi_discovery(
    configs: list[Any], *, transport: Any = None
) -> RemoteSourceDiscovery:
    """从一组 OpenAPI 配置构造发现源；transport 供测试注入 httpx.MockTransport。"""
    from yai_core.integrations.openapi.client import OpenApiToolBridge

    return RemoteSourceDiscovery(
        [OpenApiToolBridge(c, transport=transport) for c in configs]
    )
