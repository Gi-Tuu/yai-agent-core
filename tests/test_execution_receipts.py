"""执行收据与进程内幂等：状态、retryable、call_id 与重试去重。"""

from __future__ import annotations

import asyncio
import time

import pytest

from yai_core import (
    AgentCore,
    ExecutionReceipt,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    ToolGovernance,
    build_composite_spec,
    build_spec,
    check_tool_specs,
)
from yai_core.channels import CollectChannel
from yai_core.policy import AllowlistPolicy
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import EventType, governance_from_manifest, governance_to_manifest


def _executor(specs, *, timeout=None) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_many(specs)
    return ToolExecutor(
        registry,
        AllowlistPolicy((), mode="auto"),
        CollectChannel(),
        handler_timeout=timeout,
    )


def _receipt(events) -> dict:
    result = [e for e in events if e.type == EventType.TOOL_RESULT][-1]
    return result.data["receipt"]


def test_success_receipt_has_call_id_and_is_retryable_false() -> None:
    def apply(value: str = "") -> dict:
        """写入。"""
        return {"value": value}

    events, ok, _ = asyncio.run(
        _executor([build_spec(apply)]).execute("apply", {"value": "x"}, call_id="call-1")
    )
    assert ok is True
    receipt = _receipt(events)
    assert receipt["call_id"] == "call-1"
    assert receipt["status"] == "succeeded"
    assert receipt["retryable"] is False


def test_timeout_receipt_is_unknown_and_not_retryable_without_key() -> None:
    def slow() -> dict:
        """慢。"""
        time.sleep(0.2)
        return {"ok": True}

    events, ok, _ = asyncio.run(
        _executor([build_spec(slow)], timeout=0.02).execute("slow", {}, call_id="t1")
    )
    assert ok is False
    receipt = _receipt(events)
    assert receipt["status"] == "unknown"
    assert receipt["retryable"] is False


def test_idempotent_success_is_replayed_without_reaching_handler() -> None:
    calls: list[str] = []

    def apply(key: str = "") -> dict:
        """写入。"""
        calls.append(key)
        return {"key": key}

    spec = build_spec(apply)
    spec.governance = ToolGovernance(idempotency_key=lambda args, subject: args["key"])
    executor = _executor([spec])

    first_events, first_ok, _ = asyncio.run(
        executor.execute("apply", {"key": "order-1"}, call_id="c1")
    )
    second_events, second_ok, second_text = asyncio.run(
        executor.execute("apply", {"key": "order-1"}, call_id="c2")
    )

    assert first_ok is True and second_ok is True
    assert calls == ["order-1"]
    first = _receipt(first_events)
    second = _receipt(second_events)
    assert first["status"] == "succeeded" and first["replayed"] is False
    assert second["replayed"] is True
    assert second["idempotency_key"] == "order-1"
    assert second_text == '{"key": "order-1"}'


def test_idempotent_unknown_is_replayed_as_unknown_no_second_handler() -> None:
    calls: list[int] = []

    def slow(key: str = "") -> dict:
        """慢。"""
        calls.append(1)
        time.sleep(0.2)
        return {"ok": True}

    spec = build_spec(slow)
    spec.governance = ToolGovernance(idempotency_key=lambda args, subject: args["key"])
    executor = _executor([spec], timeout=0.02)

    first_events, first_ok, _ = asyncio.run(
        executor.execute("slow", {"key": "k1"}, call_id="c1")
    )
    second_events, second_ok, _ = asyncio.run(
        executor.execute("slow", {"key": "k1"}, call_id="c2")
    )

    assert first_ok is False and second_ok is False
    assert calls == [1]
    assert _receipt(first_events)["status"] == "unknown"
    assert _receipt(second_events)["replayed"] is True
    assert _receipt(second_events)["status"] == "unknown"


def test_composite_failure_after_one_success_is_partial() -> None:
    def first() -> dict:
        """第一步。"""
        return {"ok": True}

    def second() -> dict:
        """第二步。"""
        raise RuntimeError("boom")

    flow = build_composite_spec("flow", "流程", [{"tool": "first"}, {"tool": "second"}])
    events, ok, _ = asyncio.run(
        _executor([build_spec(first), build_spec(second), flow]).execute("flow", {})
    )
    assert ok is False
    receipt = _receipt(events)
    assert receipt["status"] == "partial"
    assert receipt["retryable"] is False


def test_validation_failure_is_not_started() -> None:
    def apply(amount: int) -> dict:
        """写入。"""
        return {"amount": amount}

    spec = build_spec(apply)
    spec.input_schema = {
        "type": "object",
        "properties": {"amount": {"type": "integer", "minimum": 10}},
        "required": ["amount"],
    }
    events, ok, _ = asyncio.run(_executor([spec]).execute("apply", {"amount": 1}))
    assert ok is False
    assert _receipt(events)["status"] == "not_started"


def test_idempotency_key_provider_failure_blocks_execution() -> None:
    calls: list[int] = []

    def apply() -> dict:
        """写入。"""
        calls.append(1)
        return {"ok": True}

    def broken_key(args, subject):
        raise RuntimeError("no business id")

    spec = build_spec(apply)
    spec.governance = ToolGovernance(idempotency_key=broken_key)
    events, ok, text = asyncio.run(_executor([spec]).execute("apply", {}))
    assert ok is False and calls == []
    assert "幂等键生成失败" in text
    assert _receipt(events)["status"] == "not_started"


def test_agent_loop_passes_model_call_id_into_receipt() -> None:
    def apply(value: str = "") -> dict:
        """写入。"""
        return {"value": value}

    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCallRequest(id="model-call-9", name="apply", arguments={"value": "x"})
                ]
            ),
            ModelResponse(content="完成"),
        ]
    )
    core = AgentCore(model, auto_approve_tools=True)
    core.register_tools([build_spec(apply)])
    result = asyncio.run(core.run("调用工具执行"))

    result_event = next(e for e in result.events if e.type == EventType.TOOL_RESULT)
    assert result_event.data["receipt"]["call_id"] == "model-call-9"


def test_idempotency_key_is_not_serialized_into_manifest() -> None:
    governance = ToolGovernance(idempotency_key=lambda args, subject: "k")
    manifest = governance_to_manifest(governance)
    assert "idempotency_key" not in manifest
    assert governance_from_manifest("demo", manifest).idempotency_key is None


def test_health_flags_non_callable_idempotency_key() -> None:
    spec = build_spec(lambda: {})
    spec.governance = ToolGovernance(idempotency_key="not-callable")  # type: ignore[arg-type]
    report = check_tool_specs([spec])
    assert "governance_bad_idempotency_key" in {f.code for f in report.findings}
    assert report.ok is False


def test_receipt_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="未知执行状态"):
        ExecutionReceipt(call_id="c", tool="t", status="bogus")  # type: ignore[arg-type]
