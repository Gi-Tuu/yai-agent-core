"""v0.8 远程动态发现源：MCP 目录与 OpenAPI 服务目录实现同一 ``ToolDiscovery``。

复用 integrations 里的 :class:`McpToolBridge` / :class:`OpenApiToolBridge`，把"一组外部
能力源"包装成内核可注入的发现源：

- **懒连接**：首次 ``discover`` 时才连接所有源；单个源失败 / 超时被跳过，不拖垮其余；
- **连接常驻**：bridge 的 handler 依赖底层 client，发现源保持连接并缓存 specs，不提前关闭；
- **去重**：跳过已注册工具（available）与同批重名；
- **停机**：``aclose()`` 关闭所有 bridge。

配置即白名单：宿主显式配置了哪些外部源，它们的工具才"可被发现"。``need`` 做**保守
双通道筛选**：词法（name+description 双向重叠，零依赖）始终可用；宿主再提供 embedder
时叠加语义通道（余弦 gate，跨概念高召回）。任一通道识别出明确子集才收窄（默认上限
8 个），两通道都不中就回退全量——绝不在不确定时把可能相关的工具筛掉；
``filter_by_need=False`` 可关闭、保持全量行为。注册去重由内核、执行授权由
PermissionPolicy 分别负责，发现源不越权。
"""

from __future__ import annotations

from typing import Any, Protocol

from yai_core.discovery.scoring import (
    DEFAULT_REMOTE_TOP_K,
    DEFAULT_SPEC_MARGIN,
    DEFAULT_SPEC_NEED_THRESHOLD,
    DEFAULT_SYNONYM_GROUPS,
    canonicalize,
    filter_specs_by_need,
    spec_text,
)
from yai_core.discovery.semantic import (
    DEFAULT_SCORE_THRESHOLD,
    DEFAULT_SEMANTIC_MARGIN,
    cosine_similarity,
)
from yai_core.types import ToolSpec


class _RemoteBridge(Protocol):
    """MCP / OpenAPI bridge 的最小结构（duck typing，测试可直接注入假对象）。"""

    async def connect(self) -> list[ToolSpec]: ...

    async def aclose(self) -> None: ...


class RemoteSourceDiscovery:
    """一组远程 bridge（MCP / OpenAPI）的懒连接发现源，实现 ToolDiscovery。"""

    def __init__(
        self,
        bridges: list[Any],
        *,
        filter_by_need: bool = True,
        score_threshold: float = DEFAULT_SPEC_NEED_THRESHOLD,
        score_margin: float = DEFAULT_SPEC_MARGIN,
        top_k: int = DEFAULT_REMOTE_TOP_K,
        synonym_groups: tuple[tuple[str, ...], ...] = DEFAULT_SYNONYM_GROUPS,
        embedder: Any = None,
    ) -> None:
        self._bridges = list(bridges)
        self._cache: list[ToolSpec] | None = None
        self.filter_by_need = filter_by_need
        self.score_threshold = score_threshold
        self.score_margin = score_margin
        self.top_k = top_k
        self.synonym_groups = synonym_groups
        # 可选语义通道：提供 EmbeddingProvider 时，跨概念语义匹配叠加词法（高召回）。
        self.embedder = embedder
        # 最近一次筛选观测：narrowed 是否收窄、各工具相关度，便于测试与宿主调试。
        self.last_narrowed: bool | None = None
        self.last_scores: dict[str, float] = {}

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
        deduped: list[ToolSpec] = []
        for spec in specs:
            if spec.name in already or spec.name in seen:
                continue
            seen.add(spec.name)
            deduped.append(spec)
        if not self.filter_by_need:
            self.last_narrowed = False
            self.last_scores = {}
            return deduped
        lex_selected, lex_narrowed, scores = filter_specs_by_need(
            deduped,
            need,
            task=task,
            threshold=self.score_threshold,
            top_k=self.top_k,
            margin=self.score_margin,
            groups=self.synonym_groups,
        )
        sem_selected = await self._semantic_select(deduped, need, task)
        if not lex_narrowed and not sem_selected:
            self.last_narrowed = False
            self.last_scores = scores
            return deduped  # 两通道都未识别：保守回退全量，不漏召回
        merged: list[ToolSpec] = []
        names: set[str] = set()
        # 语义命中（已按余弦降序）在前，词法收窄集补充，按 name 去重。
        for spec in [*sem_selected, *(lex_selected if lex_narrowed else [])]:
            if spec.name not in names:
                names.add(spec.name)
                merged.append(spec)
        self.last_narrowed = True
        self.last_scores = scores
        return merged

    async def _semantic_select(
        self, deduped: list[ToolSpec], need: str, task: str
    ) -> list[ToolSpec]:
        """语义 gate：embedder 缺失/失败不致命；top1 过阈值且与次选边际足够才选中。"""
        if self.embedder is None or not deduped:
            return []
        try:
            query_vec = (
                await self.embedder.embed_texts(
                    [canonicalize(f"{need}\n{task}", self.synonym_groups)]
                )
            )[0]
            spec_vecs = await self.embedder.embed_texts(
                [
                    canonicalize(spec_text(spec), self.synonym_groups)
                    for spec in deduped
                ]
            )
        except Exception:  # noqa: BLE001 - 语义通道失败退回词法/全量，不致命
            return []
        ranked = sorted(
            (
                (cosine_similarity(query_vec, vec), index, spec)
                for index, (spec, vec) in enumerate(
                    zip(deduped, spec_vecs, strict=True)
                )
            ),
            key=lambda item: (-item[0], item[1]),
        )
        if not ranked or ranked[0][0] < DEFAULT_SCORE_THRESHOLD:
            return []
        top = ranked[0][0]
        return [
            spec for score, _, spec in ranked if score >= top - DEFAULT_SEMANTIC_MARGIN
        ]

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
