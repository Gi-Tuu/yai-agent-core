"""路由学习契约（第六个 SPI）：从执行反馈中自校准路由策略。

默认**不配置**：宿主不显式注入 ``RouteSelector`` 时，路由完全走规则/LLM，
行为与未接入学习时完全一致（安全默认）。

内核提供零依赖默认实现 :class:`yai_core.learning.bandit.ContextualBanditSelector`，
宿主也可实现本协议替换为其它学习器（如在线逻辑回归、外部画像服务）。

本模块只在类型检查时引用 learning 包，运行时零导入，避免 spi 契约层与
learning 实现层产生循环依赖。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from yai_core.learning.bandit import RouteSuggestion
    from yai_core.learning.features import TaskFeatures
    from yai_core.learning.outcomes import RouteOutcome
    from yai_core.tools.registry import ToolRegistry
    from yai_core.types import Strategy


@runtime_checkable
class RouteSelector(Protocol):
    """根据任务上下文建议路由策略，并在任务结束后接收反馈。

    方法都是同步的：suggest 只做本地查表/采样，不发起网络/模型调用，
    因此可以放在路由热路径上。
    """

    def suggest(
        self,
        features: TaskFeatures,
        registry: ToolRegistry,
    ) -> RouteSuggestion | None:
        """返回建议策略；返回 None 表示学习器不表态（证据不足/硬规则区域），
        路由器应回退到规则或 LLM。"""
        ...

    def record(
        self,
        features: TaskFeatures,
        chosen: Strategy,
        outcome: RouteOutcome,
    ) -> None:
        """一次任务结束后，把"当时上下文 + 实际选择 + 事后结果"回灌学习。"""
        ...
