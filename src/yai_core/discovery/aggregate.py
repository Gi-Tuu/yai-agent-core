"""聚合多个 ``ToolDiscovery`` 为一个：内核只接受单个 discovery 参数。

宿主可同时配置进程内 :class:`StaticCatalog` 与远程 MCP / OpenAPI 源
（:class:`RemoteSourceDiscovery`），由本类对它们逐个发现、跨源去重后统一返回；
注册与授权仍由内核 / PermissionPolicy 统一处理。可选的 ``list_discoverable``
同样聚合各源声明并去重（未实现该方法的源用 getattr 探测后跳过）。
"""

from __future__ import annotations

from typing import Any

from yai_core.types import ToolSpec


class CompositeDiscovery:
    """按顺序串联多个发现源，表现为单个 ToolDiscovery。"""

    def __init__(self, sources: list[Any]) -> None:
        self._sources = list(sources)

    async def discover(
        self,
        need: str,
        *,
        task: str,
        available: list[str],
    ) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        current_available = set(available)
        for source in self._sources:
            specs = await source.discover(
                need, task=task, available=list(current_available)
            )
            for spec in specs:
                # 再判一次：把前面源已返回的名字视为"已注册"，跨源去重。
                if spec.name in current_available:
                    continue
                current_available.add(spec.name)
                out.append(spec)
        return out

    def list_discoverable(self) -> list[dict[str, str]]:
        """聚合各源的轻量声明目录并按 name 去重；未实现该方法的源跳过。"""
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for source in self._sources:
            fn = getattr(source, "list_discoverable", None)
            if not callable(fn):
                continue
            for item in fn():
                if item.get("name") in seen:
                    continue
                seen.add(item["name"])
                items.append(item)
        return items
