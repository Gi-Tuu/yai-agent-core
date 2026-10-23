"""执行边界回归：约束必须覆盖组合、代码、协程与预算的每一条实际路径。

这组用例来自一次只读审查：原有测试分别验证了"普通工具"的校验、频控与超时，
但组合工具的提前分支、同步函数返回协程、代码沙箱的声明超时都没有走同一道闸。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from yai_core import (
    AgentCore,
    Budget,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    build_composite_spec,
    build_spec,
)
from yai_core.channels import CollectChannel
from yai_core.kernel.budget import BudgetExceeded
from yai_core.policy import AllowlistPolicy
from yai_core.spi.sandbox import SandboxResult
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import (
    ApprovalPolicy,
    EventType,
    RateLimitHint,
    ToolGovernance,
    ToolSpec,
)


def _executor(*specs, sandbox=None) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_many(specs)
    return ToolExecutor(
        registry,
        AllowlistPolicy((), mode="auto"),
        CollectChannel(),
        sandbox=sandbox,
    )


def test_schema_domain_is_enforced_for_every_tool_even_without_approval() -> None:
    calls: list[int] = []

    def refund(amount: int) -> dict:
        """退款。"""
        calls.append(amount)
        return {"amount": amount}

    spec = build_spec(refund)
    spec.input_schema = {
        "type": "object",
        "properties": {"amount": {"type": "integer", "minimum": 100}},
        "required": ["amount"],
    }
    events, ok, text = asyncio.run(_executor(spec).execute("refund", {"amount": 1}))

    assert ok is False
    assert calls == []
    assert "参数不合法" in text
    result = next(e for e in events if e.type == EventType.TOOL_RESULT)
    assert result.data["invalid_arguments"] is True


def test_composite_rate_limit_blocks_before_any_step_runs() -> None:
    calls: list[int] = []

    def step(n: int) -> dict:
        """一步。"""
        calls.append(n)
        return {"n": n}

    wrapper = build_composite_spec(
        "flow", "一步流程", [{"tool": "step", "args": {"n": 1}}]
    )
    wrapper.governance = ToolGovernance(rate_limit=RateLimitHint(count=1, window="1m"))
    executor = _executor(build_spec(step), wrapper)

    _, first_ok, _ = asyncio.run(executor.execute("flow", {}))
    _, second_ok, second_text = asyncio.run(executor.execute("flow", {}))

    assert first_ok is True
    assert second_ok is False
    assert "频率上限" in second_text
    assert calls == [1]


def test_composite_declared_timeout_covers_the_whole_workflow() -> None:
    def quick() -> dict:
        """先完成一步。"""
        return {"ok": True}

    async def slow() -> dict:
        """慢步骤。"""
        await asyncio.sleep(0.2)
        return {"ok": True}

    wrapper = build_composite_spec(
        "flow", "慢流程", [{"tool": "quick"}, {"tool": "slow"}]
    )
    wrapper.governance = ToolGovernance(timeout_ms=20)
    started = time.monotonic()
    events, ok, text = asyncio.run(
        _executor(build_spec(quick), build_spec(slow), wrapper).execute("flow", {})
    )
    elapsed = time.monotonic() - started

    assert ok is False
    assert elapsed < 0.15, f"组合声明的 20ms 超时没有生效，实际 {elapsed:.3f}s"
    assert "20ms" in text
    assert any(
        e.type == EventType.TOOL_CALL and e.data.get("tool") == "quick"
        for e in events
    ), "超时前已完成步骤的审计事件不能丢"
    result = next(e for e in events if e.type == EventType.TOOL_RESULT and e.data.get("timed_out"))
    assert result.data["timed_out"] is True


def test_composite_steps_consume_task_budget() -> None:
    calls: list[str] = []

    def first() -> dict:
        """第一步。"""
        calls.append("first")
        return {"ok": True}

    def second() -> dict:
        """第二步。"""
        calls.append("second")
        return {"ok": True}

    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCallRequest(id="c1", name="flow", arguments={})
                ]
            ),
            ModelResponse(content="收尾"),
        ]
    )
    core = AgentCore(
        model, budget=Budget(max_tool_calls=1), auto_approve_tools=True
    )
    core.register_tools(
        [
            build_spec(first),
            build_spec(second),
            build_composite_spec(
                "flow", "两步流程", [{"tool": "first"}, {"tool": "second"}]
            ),
        ]
    )

    result = asyncio.run(core.run("调用工具执行两步流程"))

    assert calls == ["first"], "预算 1 只能允许第一个真实步骤"
    assert any(e.type == EventType.BUDGET_EXCEEDED for e in result.events)
    assert next(e for e in result.events if e.type == EventType.DONE).data[
        "reason"
    ] == "budget_exceeded"


def test_direct_executor_call_raises_budget_exceeded_instead_of_swallowing() -> None:
    calls: list[int] = []

    def step() -> dict:
        """一步。"""
        calls.append(1)
        return {"ok": True}

    wrapper = build_composite_spec(
        "flow", "两步流程", [{"tool": "step"}, {"tool": "step"}]
    )
    executor = _executor(build_spec(step), wrapper)
    tracker = Budget(max_tool_calls=1).start()

    with pytest.raises(BudgetExceeded):
        asyncio.run(executor.execute("flow", {}, budget=tracker))
    assert calls == [1]


def test_sync_handler_returning_awaitable_still_honours_timeout() -> None:
    async def work() -> dict:
        await asyncio.sleep(0.2)
        return {"ok": True}

    def returns_awaitable():
        """同步函数返回协程（少见但合法）。"""
        return work()

    spec = build_spec(returns_awaitable)
    spec.governance = ToolGovernance(timeout_ms=20)
    started = time.monotonic()
    _, ok, text = asyncio.run(_executor(spec).execute("returns_awaitable", {}))
    elapsed = time.monotonic() - started

    assert ok is False
    assert elapsed < 0.15, f"返回协程后绕过了超时，实际 {elapsed:.3f}s"
    assert "20ms" in text or "超过 0.02s" in text


def test_code_tool_declared_timeout_is_forwarded_to_sandbox() -> None:
    class Sandbox:
        def __init__(self) -> None:
            self.timeout: float | None = None

        async def execute(self, code, inputs, *, timeout=None):
            self.timeout = timeout
            return SandboxResult(ok=True, output={})

    sandbox = Sandbox()
    spec = ToolSpec(
        name="code_probe",
        description="探针",
        input_schema={"type": "object", "properties": {}},
        handler=None,
        source="code",
        code="",
        governance=ToolGovernance(timeout_ms=10),
    )
    _, ok, _ = asyncio.run(
        _executor(spec, sandbox=sandbox).execute("code_probe", {})
    )

    assert ok is True
    assert sandbox.timeout == 0.01


def test_token_total_uses_this_response_delta_when_total_is_missing() -> None:
    from yai_core.types import ModelResponse as Response
    from yai_core.types import TokenUsage

    tracker = Budget(max_tokens=1000).start()
    totals: list[int] = []
    for _ in range(3):
        tracker.record_model_call(
            Response(usage=TokenUsage(prompt_tokens=7, completion_tokens=3))
        )
        totals.append(tracker.total_tokens)

    assert totals == [10, 20, 30]


def test_composite_version_is_a_deep_snapshot_and_restores_constraints() -> None:
    from yai_core.tools.composer import CompositeWorkflowStore

    spec = build_composite_spec(
        "flow",
        "审批流程",
        [{"tool": "step", "args": {"nested": {"value": 10}}}],
        input_schema={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    )
    spec.governance = ToolGovernance(
        sensitive=("x",),
        approval=ApprovalPolicy(required=True),
        timeout_ms=500,
    )
    store = CompositeWorkflowStore()
    store.save(spec, version="v1")

    spec.steps[0].args["nested"]["value"] = 20
    record = store.get("flow", "v1")
    assert record.steps[0].args["nested"]["value"] == 10

    exported = record.to_manifest_steps()
    exported[0]["args"]["nested"]["value"] = 30
    assert store.get("flow", "v1").steps[0].args["nested"]["value"] == 10

    restored = store.restore("flow", "v1")
    assert restored.input_schema == spec.input_schema
    assert restored.governance.approval is not None
    assert restored.governance.approval.required is True
    assert restored.governance.sensitive == ("x",)
    assert restored.governance.timeout_ms == 500
    assert restored.governance.gate is None

    restored.steps[0].args["nested"]["value"] = 40
    assert store.get("flow", "v1").steps[0].args["nested"]["value"] == 10
