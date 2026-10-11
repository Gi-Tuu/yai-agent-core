"""TierPolicy 纯策略单测：成本·延迟感知选档，零依赖、确定性。

口径：默认 standard（便宜快），规划 / 工具失败纠偏 / 执行到后段仍未完成才
升 strong（贵但强）。这里只测纯决策逻辑，不进入 Agent Loop、不调模型。
"""

from yai_core.kernel import STANDARD, STRONG, TierPolicy


def test_default_planning_uses_strong() -> None:
    """生成多步计划属于重拆解，默认用 strong。"""
    decision = TierPolicy().for_planning()
    assert decision.tier == STRONG
    assert decision.reason  # 理由非空，保证可审计


def test_select_standard_for_early_iterations() -> None:
    """前 3 轮（iteration 0/1/2）无失败时恒 standard，省钱。"""
    policy = TierPolicy()
    for iteration in (0, 1, 2):
        decision = policy.select(
            strategy="react", iteration=iteration, had_failure=False
        )
        assert decision.tier == STANDARD


def test_select_strong_after_escalation_iteration() -> None:
    """执行到第 3 轮仍未完成，判定为卡住，升 strong 推进。"""
    policy = TierPolicy()
    decision = policy.select(
        strategy="react", iteration=3, had_failure=False
    )
    assert decision.tier == STRONG


def test_select_strong_on_failure() -> None:
    """工具刚失败后的纠偏轮，即使在第 0 轮也升 strong。"""
    policy = TierPolicy()
    decision = policy.select(
        strategy="react", iteration=0, had_failure=True
    )
    assert decision.tier == STRONG


def test_planning_uses_default_when_strong_for_planning_disabled() -> None:
    """关闭 strong_for_planning 后，规划轮也用默认档（standard）。"""
    policy = TierPolicy(strong_for_planning=False)
    assert policy.for_planning().tier == STANDARD


def test_failure_not_escalated_when_strong_on_failure_disabled() -> None:
    """同时关闭失败升档与迭代升档后，失败也留在 standard。"""
    policy = TierPolicy(strong_on_failure=False, escalate_after_iter=-1)
    decision = policy.select(
        strategy="react", iteration=0, had_failure=True
    )
    assert decision.tier == STANDARD


def test_iteration_not_escalated_when_threshold_disabled() -> None:
    """escalate_after_iter=-1 表示不因迭代轮数升档。"""
    policy = TierPolicy(escalate_after_iter=-1)
    decision = policy.select(
        strategy="react", iteration=9, had_failure=False
    )
    assert decision.tier == STANDARD


def test_custom_default_tier() -> None:
    """宿主可把默认档改成 strong（重任务场景）。"""
    policy = TierPolicy(default_tier=STRONG, strong_for_planning=False)
    decision = policy.select(
        strategy="react", iteration=0, had_failure=False
    )
    assert decision.tier == STRONG
