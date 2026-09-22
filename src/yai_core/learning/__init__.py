"""自校准路由学习层：从执行反馈中学习"什么任务该用什么策略"。

纯标准库、零第三方依赖，默认不接线（不配置 RouteSelector 时内核行为不变）。
- :class:`~yai_core.learning.features.TaskFeatures`：任务上下文特征。
- :func:`~yai_core.learning.outcomes.extract_route_outcome`：从事后事件流抽取奖励。
- :class:`~yai_core.learning.bandit.ContextualBanditSelector`：上下文老虎机选择器
  （实现了第六个 SPI：``yai_core.spi.RouteSelector``）。
"""

from yai_core.learning.bandit import ContextualBanditSelector, RouteSuggestion
from yai_core.learning.features import TaskFeatures
from yai_core.learning.outcomes import RouteOutcome, extract_route_outcome

__all__ = [
    "TaskFeatures",
    "RouteOutcome",
    "extract_route_outcome",
    "RouteSuggestion",
    "ContextualBanditSelector",
]
