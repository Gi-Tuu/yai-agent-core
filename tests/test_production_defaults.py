"""生产安全默认值测试：allow_all 默认必须可见（警告），显式配置不警告。

对应 v0.25 审计 D-1：`auto_approve_tools=True` + `allow_all` 是开发形态默认值，
本轮不改行为，但风险必须说在明面上（ProductionSafetyWarning）。
"""

from __future__ import annotations

import asyncio
import warnings
from pathlib import Path

from yai_core import (
    AgentCore,
    EventType,
    ProductionSafetyWarning,
    ScriptedModel,
    build_spec,
)
from yai_core.policy import AllowlistPolicy
from yai_core.policy.approval import ApprovalPolicy, ApprovalRule
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import ToolGovernance


def _core(**kwargs) -> AgentCore:
    kwargs.setdefault("model", ScriptedModel("ok"))
    return AgentCore(**kwargs)


def test_default_config_warns_production_safety() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _core()
    assert any(issubclass(w.category, ProductionSafetyWarning) for w in caught), (
        "默认 allow_all 配置必须发生产安全警告"
    )
    assert any("docs/production-permissions.md" in str(w.message) for w in caught)


def test_explicit_policy_does_not_warn() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _core(policy=AllowlistPolicy(("count_notes",), mode="auto"))
    assert not [w for w in caught if issubclass(w.category, ProductionSafetyWarning)]


def test_auto_approve_false_does_not_warn() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _core(auto_approve_tools=False)
    assert not [w for w in caught if issubclass(w.category, ProductionSafetyWarning)]


def test_warning_not_raised_as_error_by_default_filters() -> None:
    # 默认过滤器下构造成功（UserWarning 不是 Error）：行为零变化。
    core = _core()
    assert core.policy is not None


def test_production_policy_example_builds_and_enforces() -> None:
    """docs/production-permissions.md 的配套示例可构建且审批策略生效（防文档漂移）。"""
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "examples"))
    try:
        from production_policy import build_core
    finally:
        sys.path.pop(0)

    core = build_core()
    # 读操作在白名单内：直接放行；写操作不在：走审批（CollectChannel 自动确认）
    assert core.policy is not None


def test_expense_over_limit_requires_approval() -> None:
    """参数级审批：amount > 1000 的提交必须人工确认（示例策略的生效证明）。"""

    def submit_expense(amount: int, purpose: str) -> str:
        """提交报销单。"""
        return f"已提交：{purpose} {amount}"

    spec = build_spec(submit_expense)
    spec.governance = ToolGovernance(
        approval=ApprovalPolicy(when=(ApprovalRule("amount", ">", 1000),)),
    )
    registry = ToolRegistry()
    registry.register_many([spec])
    executor = ToolExecutor(
        registry, AllowlistPolicy((), mode="allow_all"),
        _approval_channel(),
    )
    events, ok, _text = asyncio.run(
        executor.execute(
            "submit_expense", {"amount": 5000, "purpose": "设备"}, call_id="c1"
        )
    )
    assert not ok
    assert any(e.type == EventType.PERMISSION_ASKED for e in events)


def _approval_channel() -> object:
    """审批通道：人类审批人拒绝超额报销（CollectChannel 自动确认不适用）。"""
    from yai_core.channels import CollectChannel

    class _DenyChannel(CollectChannel):
        async def confirm(self, tool_name: str, arguments: dict) -> bool:
            return False

    return _DenyChannel()
