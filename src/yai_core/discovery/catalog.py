"""进程内静态工具目录：最小、零依赖、离线可测的 ToolDiscovery 实现。

宿主把"暂不默认启用、但可按需长出"的能力预先登记为候选（函数 + 关键词）。
能力缺口出现时，用缺口描述与原始任务做关键词匹配，命中才把候选构造成
ToolSpec 返回——这是"发现工具"的第一步：

- 不访问网络、不依赖任何市场，纯标准库，不破坏内核零依赖红线；
- 宿主把哪些函数放进目录，本身就是一层授权（目录里没有的能力永远不会被发现）；
- 发现后的工具仍要经过 PermissionPolicy 才能执行，目录不绕过授权。

后续 MCP 目录 / OpenAPI 服务发现可实现同一个 ToolDiscovery 协议，
内核的发现闭环无需改动。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from yai_core.discovery.introspect import build_spec
from yai_core.types import ToolSpec


@dataclass
class DiscoveredCandidate:
    """一个"按需启用"的候选能力。

    Attributes:
        fn: 候选能力对应的普通 Python 函数（与内省注册的能力同构）。
        keywords: 命中缺口/任务的关键词（中文子串或英文小写子串）。
            必须至少提供一个关键词，否则该候选永不被发现（避免无条件匹配）。
        name: 可选自定义工具名；缺省用函数名。
        description: 可选描述覆盖；缺省用函数 docstring 首段。
    """

    fn: Callable[..., Any]
    keywords: tuple[str, ...]
    name: str | None = None
    description: str | None = None

    def spec_name(self) -> str:
        return self.name or self.fn.__name__


class StaticCatalog:
    """进程内候选目录：按关键词匹配缺口，返回命中的 ToolSpec。"""

    def __init__(self, candidates: Iterable[DiscoveredCandidate]) -> None:
        self._candidates = list(candidates)

    async def discover(
        self,
        need: str,
        *,
        task: str,
        available: list[str],
    ) -> list[ToolSpec]:
        # 缺口描述与原始任务都纳入匹配，中文按子串、英文统一小写。
        haystack = f"{need}\n{task}".lower()
        already = set(available)
        specs: list[ToolSpec] = []
        seen: set[str] = set()
        for cand in self._candidates:
            name = cand.spec_name()
            if name in already or name in seen:
                continue
            keywords = [k.lower() for k in cand.keywords if k and k.strip()]
            if not keywords:
                # 没有关键词的候选不允许无条件命中，防止误注册。
                continue
            if not any(k in haystack for k in keywords):
                continue
            spec = build_spec(cand.fn, name=cand.name)
            if cand.description:
                spec.description = cand.description
            specs.append(spec)
            seen.add(name)
        return specs
