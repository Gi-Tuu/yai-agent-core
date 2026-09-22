"""路由反馈：从一次 run 的事件流里事后抽取"这次路由选对了吗"。

纯函数、零模型调用、离线可测。内核在执行过程中已经免费产出了完整的
:class:`~yai_core.types.AgentEvent` 流，这里把它压成 contextual bandit 需要的
奖励信号，是开环路由走向闭环自校准的关键。

学习只发生在任务结束后（run 结束才调用一次），不影响当次执行的稳定性。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from yai_core.types import AgentEvent, EventType, Strategy

#: 最终文案里"模型承认自己没能力完成"的直接信号短语。
#: direct 误判时最典型：任务本该调工具，却被路由成纯对话，模型只能回答"无法/没有数据"。
_UNABLE_HINTS = (
    "无法",
    "不能访问",
    "无法访问",
    "无法获取",
    "无法查询",
    "无法读取",
    "没有权限",
    "没有数据",
    "没有找到",
    "未找到",
    "查不到",
    "查询不到",
    "获取不到",
    "访问不到",
    "缺少",
    "请你手动",
    "请手动",
    "i can't",
    "i cannot",
    "cannot access",
    "can't access",
    "unable to",
)

#: 否定词 × 能力对象词的组合，覆盖"我没有这个数据"这类被修饰语隔开的话术。
_UNABLE_NEGATIONS = ("没有", "缺少", "没找到", "未能")
_UNABLE_OBJECTS = ("数据", "权限", "记录", "信息", "访问", "资料", "结果", "内容", "能力")

#: 成本权重（经验默认值，可在接线时注入；benchmark 会做敏感性分析）。
#: 成本只在"同一上下文内都能成功"的策略之间做区分，不该压过成败信号。
W_TOOL = 0.05
W_PLAN = 0.10
W_CLAR = 0.15

_DEFAULT_TOOL_BUDGET = 6
_DEFAULT_CLAR_BUDGET = 2


@dataclass(frozen=True)
class RouteOutcome:
    """一次路由决策的事后结果。

    既保留可解释的原始信号（工具失败数、澄清轮数、是否撞迭代上限…），
    也给出 bandit 直接消费的标量 ``reward``（裁剪到 [0, 1]）。
    """

    success: bool
    reward: float
    tool_calls: int = 0
    tool_failures: int = 0
    clarify_rounds: int = 0
    plan_steps: int = 0
    hit_max_iters: bool = False
    aborted: bool = False
    errors: int = 0
    final_empty: bool = False
    unable_hint: bool = False

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "reward": self.reward,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "clarify_rounds": self.clarify_rounds,
            "plan_steps": self.plan_steps,
            "hit_max_iters": self.hit_max_iters,
            "aborted": self.aborted,
            "errors": self.errors,
            "final_empty": self.final_empty,
            "unable_hint": self.unable_hint,
        }


def _hit_unable(text: str) -> bool:
    lowered = text.lower()
    if any(
        (h in lowered) if h.isascii() else (h in text)
        for h in _UNABLE_HINTS
    ):
        return True
    # 否定词与能力对象词同时出现（如"我没有这个数据"），覆盖被修饰语隔开的情况。
    has_negation = any(w in text for w in _UNABLE_NEGATIONS)
    has_object = any(w in text for w in _UNABLE_OBJECTS)
    return has_negation and has_object


def extract_route_outcome(
    events: Sequence[AgentEvent],
    chosen: Strategy,
    *,
    max_iters: int = _DEFAULT_TOOL_BUDGET,
    clarify_budget: int = _DEFAULT_CLAR_BUDGET,
) -> RouteOutcome:
    """从事件流抽取路由结果。

    Args:
        events: 本次 run 的完整事件流（``AgentCore.astream`` 产出）。
        chosen: 本次实际采用的策略（来自 STRATEGY_SELECTED / 降级后的最终策略）。
        max_iters: react 工具轮数预算，用于归一化工具成本并启发式判断撞满。
        clarify_budget: 澄清轮数预算，用于归一化澄清成本。

    判定口径：
        - ``aborted``：DONE 以 clarify 收尾。正常澄清成功后会递归，最终 DONE 是
          解析后的真实策略；只有澄清被放弃（空回答）才以 clarify 收尾。
        - ``hit_max_iters``：工具调用数达到迭代预算（启发式，可能略高估，仅作成本信号）。
        - ``unable_hint`` 只在 direct 下判失败——其他策略若真无能，通常已伴随
          工具失败/缺口事件，被 ``tool_failures`` 覆盖。
    """
    tool_calls = 0
    tool_failures = 0
    clarify_rounds = 0
    errors = 0
    plan_steps = 0
    final_text = ""
    done_strategy: str | None = None

    for ev in events:
        data = ev.data or {}
        if ev.type == EventType.TOOL_CALL:
            tool_calls += 1
        elif ev.type == EventType.TOOL_RESULT:
            if data.get("ok") is False:
                tool_failures += 1
        elif ev.type == EventType.CLARIFY_REQUESTED:
            clarify_rounds += 1
        elif ev.type == EventType.PLAN_CREATED:
            steps = data.get("steps") or []
            plan_steps = max(plan_steps, len(steps))
        elif ev.type == EventType.ERROR:
            errors += 1
        elif ev.type == EventType.MODEL_MESSAGE:
            if data.get("text"):
                final_text = data["text"]
        elif ev.type == EventType.DONE:
            done_strategy = data.get("strategy")
            if data.get("final_text"):
                final_text = data["final_text"]

    final = final_text.strip()
    final_empty = not final
    unable_hint = _hit_unable(final)
    aborted = done_strategy == Strategy.CLARIFY.value
    hit_max_iters = max_iters > 0 and tool_calls >= max_iters

    success = (
        not aborted
        and not hit_max_iters
        and not final_empty
        and tool_failures == 0
        and errors == 0
        and not (chosen == Strategy.DIRECT and unable_hint)
    )

    # 成本：工具用量 / 计划开销 / 澄清开销；都在 [0,1] 内归一化。
    tool_ratio = min(tool_calls / max_iters, 1.0) if max_iters > 0 else 0.0
    clar_ratio = (
        min(clarify_rounds / clarify_budget, 1.0) if clarify_budget > 0 else 0.0
    )
    cost = W_TOOL * tool_ratio + W_PLAN * (1.0 if plan_steps > 0 else 0.0) + W_CLAR * clar_ratio

    reward = (1.0 if success else 0.0) - cost
    reward = max(0.0, min(1.0, reward))

    return RouteOutcome(
        success=success,
        reward=reward,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
        clarify_rounds=clarify_rounds,
        plan_steps=plan_steps,
        hit_max_iters=hit_max_iters,
        aborted=aborted,
        errors=errors,
        final_empty=final_empty,
        unable_hint=unable_hint,
    )
