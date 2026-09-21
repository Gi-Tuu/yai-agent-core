"""工具发现契约：能力缺口出现时，按需发现并返回候选工具。

这是继 model / channel / memory / policy 之后的第五个 SPI。
默认不配置（安全默认）：只有宿主显式注入一个发现源，内核才会在运行时
把"能力缺口"变成"新工具"。发现源可以是：

- 进程内静态目录（``yai_core.discovery.StaticCatalog``，零依赖、离线可测）；
- MCP 目录搜索 / OpenAPI 服务发现（后续版本，按需懒加载）；
- 宿主自己的插件市场（AMBRACE 等）。

发现的工具仍要经过统一的权限策略（``PermissionPolicy``）才能执行，
"能被发现"不等于"被授权执行"。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from yai_core.types import ToolSpec


@runtime_checkable
class ToolDiscovery(Protocol):
    async def discover(
        self,
        need: str,
        *,
        task: str,
        available: list[str],
    ) -> list[ToolSpec]:
        """根据缺口描述返回候选工具。

        Args:
            need: 缺失能力的自然语言描述（来自路由的 missing_capability）。
            task: 触发缺口的原始任务文本。
            available: 当前已注册的工具名；发现源应跳过这些，避免重复注册。

        Returns:
            可立即注册的 ToolSpec 列表；返回空列表表示没有候选。
            只负责"发现并构造规格"，不负责注册与授权（由内核统一处理）。
        """
        ...
