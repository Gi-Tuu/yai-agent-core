"""路由学习契约（第六个 SPI）：从执行反馈中自校准路由策略。

默认**不配置**：宿主不显式注入 ``RouteSelector`` 时，路由完全走规则/LLM，
行为与未接入学习时完全一致（安全默认）。

内核提供零依赖默认实现 :class:`yai_core.learning.bandit.ContextualBanditSelector`，
宿主也可实现本协议替换为其它学习器（如在线逻辑回归、外部画像服务）。

契约方法以**原始任务文本 + 工具注册表**为入参，而不是 learning 包的特征对象：
kernel 路由层只依赖本契约（spi），不反向依赖 learning 实现层，从而避免
``learning → kernel.router`` 与 ``router → learning`` 的循环导入；特征如何抽取
是学习器实现自己的内部事务。

本模块只在类型检查时引用 learning 包，运行时零导入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from yai_core.learning.bandit import RouteSuggestion
    from yai_core.learning.outcomes import RouteOutcome
    from yai_core.tools.registry import ToolRegistry
    from yai_core.types import Strategy


@runtime_checkable
class RouteSelector(Protocol):
    """根据任务建议路由策略，并在任务结束后接收反馈。

    方法都是同步的：suggest 只做本地查表/采样，不发起网络/模型调用，
    因此可以放在路由热路径上。
    """

    def suggest(
        self,
        task: str,
        registry: ToolRegistry,
    ) -> RouteSuggestion | None:
        """返回建议策略；返回 None 表示学习器不表态（证据不足/硬规则区域），
        路由器应回退到规则或 LLM。"""
        ...

    def record(
        self,
        task: str,
        registry: ToolRegistry,
        chosen: Strategy,
        outcome: RouteOutcome,
    ) -> None:
        """一次任务结束后，把"原始任务 + 实际选择 + 事后结果"回灌学习。

        学习器内部自行用与 suggest 相同的方式抽取特征（纯函数、幂等）。
        """
        ...
