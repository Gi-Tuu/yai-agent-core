"""成本·延迟感知的模型选档策略（零第三方硬依赖）。

默认"省钱优先"：绝大多数执行轮走便宜快档（standard），只在真正需要强推理时
（生成/修订计划、工具失败纠偏、多轮后仍未完成）才升强档（strong）。规则是
确定性的、可离线测试，**选档本身不额外调用模型、不花钱**。宿主可用构造参数
收紧/放开规则；关闭成本感知（AgentCore(cost_aware=False)）时执行轮恒走
standard，行为与旧版一致。

档位与具体模型的映射由 ModelProvider 决定（如 OpenAICompatProvider：
standard -> model、strong -> strong_model，未配置 strong 时安全回退同档）。
"""

from __future__ import annotations

from dataclasses import dataclass

from yai_core.types import Strategy

#: 便宜快档（日常工具调用 / 简单问答）。
STANDARD = "standard"
#: 强档（复杂拆解 / 纠偏 / 后段卡住）。
STRONG = "strong"


@dataclass(frozen=True)
class TierDecision:
    """一次选档结果：档位 + 可审计理由。"""

    tier: str
    reason: str


class TierPolicy:
    """按任务策略 / 轮次 / 失败状态决定本轮模型档位。

    Args:
        default_tier: 无特殊情况时的档位（默认 standard，省钱、低延迟）。
        strong_for_planning: 生成 / 修订计划时用 strong（复杂步骤拆解）。
        strong_on_failure: 工具失败后的纠偏轮用 strong，提高一次改对的概率。
        escalate_after_iter: 执行到第几轮（从 0 起）仍未完成则升 strong，
            表示任务比预期复杂；设为负数等于关闭这条规则。
    """

    def __init__(
        self,
        *,
        default_tier: str = STANDARD,
        strong_for_planning: bool = True,
        strong_on_failure: bool = True,
        escalate_after_iter: int = 3,
    ) -> None:
        self.default_tier = default_tier
        self.strong_for_planning = strong_for_planning
        self.strong_on_failure = strong_on_failure
        self.escalate_after_iter = escalate_after_iter

    def for_planning(self) -> TierDecision:
        """生成 / 修订计划时的档位（默认 strong）。"""
        if self.strong_for_planning:
            return TierDecision(STRONG, "生成/修订计划需要复杂拆解，用强档")
        return TierDecision(self.default_tier, "规划不升档，用默认档")

    def select(
        self,
        *,
        strategy: Strategy,
        iteration: int,
        had_failure: bool,
    ) -> TierDecision:
        """决定一个执行轮的档位；规则按"该花才花"保守升级。"""
        if had_failure and self.strong_on_failure:
            return TierDecision(
                STRONG, "工具失败后的纠偏轮，升强档以提高一次改对的概率"
            )
        if self.escalate_after_iter >= 0 and iteration >= self.escalate_after_iter:
            return TierDecision(
                STRONG,
                f"执行到第 {iteration} 轮仍未完成，任务比预期复杂，升强档",
            )
        return TierDecision(self.default_tier, "常规执行轮，用便宜快档")
