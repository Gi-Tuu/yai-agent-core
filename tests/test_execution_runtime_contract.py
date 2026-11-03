"""执行器治理回归：异常失败关闭、rate-limit 顺序、未知副作用文案、单 run 收据链。

对应 2026-11 审计的 CORE-3 / rate-limit 顺序 / handler 异常文案 / F11。
"""

from __future__ import annotations

import asyncio

from yai_core import ToolExecutor, ToolGovernance, ToolSpec, verify_receipt_chain
from yai_core.channels import CollectChannel
from yai_core.policy import AllowlistPolicy
from yai_core.policy.approval import evaluate_approval
from yai_core.spi import PermissionDecision
from yai_core.tools import ToolRegistry
from yai_core.types import (
    ApprovalPolicy,
    ApprovalRule,
    EventType,
    RateLimitHint,
)


def _executor(*specs: ToolSpec, policy=None, channel=None) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_many(list(specs))
    return ToolExecutor(
        registry,
        policy or AllowlistPolicy(mode="allow_all"),
        channel or CollectChannel(),
    )


def _receipts(events) -> list[dict]:
    return [
        event.data["receipt"]
        for event in events
        if event.type == EventType.TOOL_RESULT
        and isinstance(event.data.get("receipt"), dict)
    ]


def test_gate_exception_fails_closed_to_tool_result() -> None:
    def boom(_arguments, _subject):
        raise RuntimeError("gate exploded")

    spec = ToolSpec(
        name="guarded",
        description="",
        input_schema={"type": "object", "properties": {}},
        handler=lambda: {"ok": True},
        governance=ToolGovernance(gate=boom),
    )
    executor = _executor(spec)
    events, ok, text = asyncio.run(executor.execute("guarded", {}))

    assert ok is False
    assert "准入闸异常" in text
    assert any(event.type == EventType.PERMISSION_DENIED for event in events)
    tool_results = [e for e in events if e.type == EventType.TOOL_RESULT]
    assert tool_results and tool_results[-1].data["ok"] is False
    receipts = _receipts(events)
    assert receipts and receipts[-1]["status"] == "not_started"


def test_policy_exception_fails_closed_instead_of_aborting() -> None:
    class BoomPolicy:
        async def check(self, _tool_name, _arguments):
            raise RuntimeError("policy backend down")

    spec = ToolSpec(
        name="policy_boom",
        description="",
        input_schema={"type": "object", "properties": {}},
        handler=lambda: {"ok": True},
    )
    executor = _executor(spec, policy=BoomPolicy())
    events, ok, text = asyncio.run(executor.execute("policy_boom", {}))

    assert ok is False
    assert "权限检查异常" in text
    assert any(e.type == EventType.PERMISSION_DENIED for e in events)
    assert _receipts(events)[-1]["status"] == "not_started"


def test_rate_limit_is_not_consumed_by_denied_calls() -> None:
    calls: list[int] = []

    def handler() -> dict:
        calls.append(1)
        return {"ok": True}

    class DenyOncePolicy:
        def __init__(self) -> None:
            self.checks = 0

        async def check(self, _tool_name, _arguments):
            self.checks += 1
            return (
                PermissionDecision.DENY
                if self.checks == 1
                else PermissionDecision.ALLOW
            )

    spec = ToolSpec(
        name="limited",
        description="",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
        governance=ToolGovernance(rate_limit=RateLimitHint(count=1, window="1m")),
    )
    executor = _executor(spec, policy=DenyOncePolicy())

    denied_events, denied_ok, _ = asyncio.run(executor.execute("limited", {}))
    assert denied_ok is False
    assert not calls, "被拒绝的调用不应扣配额，更不应执行 handler"

    ok_events, ok, _ = asyncio.run(executor.execute("limited", {}))
    assert ok is True and calls == [1]

    limited_events, limited_ok, limited_text = asyncio.run(
        executor.execute("limited", {})
    )
    assert limited_ok is False
    assert "频率上限" in limited_text
    assert any(
        e.type == EventType.TOOL_RESULT and e.data.get("rate_limited")
        for e in limited_events
    )
    assert calls == [1], "第三次调用应被限流，handler 不得再执行"


def test_handler_exception_warns_about_unknown_side_effects() -> None:
    def handler() -> dict:
        raise RuntimeError("network write failed after commit")

    spec = ToolSpec(
        name="side_effect",
        description="",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
    )
    executor = _executor(spec)
    events, ok, text = asyncio.run(executor.execute("side_effect", {}))
    assert ok is False
    assert "副作用可能已发生" in text
    assert "不要盲目重试" in text
    assert _receipts(events)[-1]["status"] == "unknown"


def test_begin_run_resets_receipt_chain_per_run() -> None:
    spec = ToolSpec(
        name="ok",
        description="",
        input_schema={"type": "object", "properties": {}},
        handler=lambda: {"ok": True},
    )
    executor = _executor(spec)
    executor.begin_run()
    first, first_ok, _ = asyncio.run(executor.execute("ok", {}))
    assert first_ok is True
    executor.begin_run()
    second, second_ok, _ = asyncio.run(executor.execute("ok", {}))
    assert second_ok is True

    first_chain = _receipts(first)
    second_chain = _receipts(second)
    assert first_chain and second_chain
    assert first_chain[0]["prev_hash"] == ""
    assert second_chain[0]["prev_hash"] == ""
    assert verify_receipt_chain(first_chain) is True
    assert verify_receipt_chain(second_chain) is True


def test_approval_schema_boundaries_fail_closed_or_require_approval() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "minLength": 3, "maxLength": 5}},
    }
    policy = ApprovalPolicy(
        when=(ApprovalRule(param="name", op="==", value="abcd"),)
    )

    invalid = evaluate_approval(policy, {"name": "ab"}, schema)
    assert invalid.decision == "reject_invalid_arguments"

    valid = evaluate_approval(policy, {"name": "abcd"}, schema)
    assert valid.decision == "approval_required"
