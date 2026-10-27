"""执行收据与进程内幂等：状态、retryable、call_id 与重试去重。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest

from yai_core import (
    AgentCore,
    ExecutionReceipt,
    ModelResponse,
    SandboxResult,
    ScriptedModel,
    ToolCallRequest,
    ToolGovernance,
    build_composite_spec,
    build_spec,
    check_tool_specs,
    verify_receipt_chain,
)
from yai_core.channels import CollectChannel
from yai_core.policy import AllowlistPolicy
from yai_core.spi import PermissionDecision
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import (
    ApprovalPolicy,
    EventType,
    GateDecision,
    governance_from_manifest,
    governance_to_manifest,
)


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


def test_idempotent_replay_is_blocked_by_current_deny_policy() -> None:
    calls: list[int] = []

    class SwitchPolicy:
        deny = False

        async def check(self, name: str, arguments: dict) -> PermissionDecision:
            return PermissionDecision.DENY if self.deny else PermissionDecision.ALLOW

    def apply(key: str = "") -> dict:
        """写入。"""
        calls.append(1)
        return {"key": key}

    spec = build_spec(apply)
    spec.governance = ToolGovernance(idempotency_key=lambda args, subject: args["key"])
    registry = ToolRegistry()
    registry.register_many([spec])
    policy = SwitchPolicy()
    executor = ToolExecutor(registry, policy, CollectChannel())

    _, first_ok, _ = asyncio.run(executor.execute("apply", {"key": "k1"}, call_id="c1"))
    assert first_ok is True

    policy.deny = True
    _, replay_ok, reason = asyncio.run(
        executor.execute("apply", {"key": "k1"}, call_id="c2")
    )
    assert replay_ok is False
    assert "权限策略拒绝" in reason
    assert calls == [1], "被拒绝的重放不能进入 handler"


def test_same_idempotency_key_with_different_arguments_is_rejected() -> None:
    calls: list[str] = []

    def apply(key: str = "", value: str = "") -> dict:
        """写入。"""
        calls.append(value)
        return {"value": value}

    spec = build_spec(apply)
    spec.governance = ToolGovernance(idempotency_key=lambda args, subject: args["key"])
    executor = _executor([spec])

    _, first_ok, _ = asyncio.run(
        executor.execute("apply", {"key": "k1", "value": "a"}, call_id="c1")
    )
    assert first_ok is True
    second_events, second_ok, second_text = asyncio.run(
        executor.execute("apply", {"key": "k1", "value": "b"}, call_id="c2")
    )

    assert second_ok is False
    assert calls == ["a"], "撞键的不同参数不能被静默当成重放"
    assert "另一组参数" in second_text
    assert _receipt(second_events)["status"] == "not_started"
    result = [e for e in second_events if e.type == EventType.TOOL_RESULT][-1]
    assert result.data["idempotency_conflict"] is True


def test_replay_does_not_ask_for_approval_again() -> None:
    calls: list[str] = []

    def apply(key: str = "") -> dict:
        """写入。"""
        calls.append(key)
        return {"key": key}

    spec = build_spec(apply)
    spec.governance = ToolGovernance(idempotency_key=lambda args, subject: args["key"])
    registry = ToolRegistry()
    registry.register_many([spec])
    executor = ToolExecutor(
        registry, AllowlistPolicy((), mode="auto"), CollectChannel(auto_confirm=True)
    )

    first_events, first_ok, _ = asyncio.run(
        executor.execute("apply", {"key": "k1"}, call_id="c1")
    )
    assert first_ok is True
    assert any(e.type == EventType.PERMISSION_ASKED for e in first_events)

    second_events, second_ok, _ = asyncio.run(
        executor.execute("apply", {"key": "k1"}, call_id="c2")
    )
    assert second_ok is True
    assert calls == ["k1"]
    assert not any(e.type == EventType.PERMISSION_ASKED for e in second_events)
    assert _receipt(second_events)["replayed"] is True


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


def _assert_replay(first, second, calls, *, ok, status) -> None:
    first_events, first_ok, first_text = first
    second_events, second_ok, second_text = second
    assert first_ok is second_ok is ok
    assert first_text == second_text
    assert calls == [1], "缓存重放不得重新执行 handler / 沙箱 / 组合步骤"
    before, after = _receipt(first_events), _receipt(second_events)
    assert before["status"] == after["status"] == status
    assert before["call_id"] == "c1" and after["call_id"] == "c2"
    assert before["replayed"] is False and after["replayed"] is True
    for field in ("retryable", "idempotency_key", "error"):
        assert before[field] == after[field]
    assert not any(e.type == EventType.TOOL_CALL for e in second_events)


@pytest.mark.parametrize("source", ["native", "code", "composite"])
@pytest.mark.parametrize("audit", [False, True])
@pytest.mark.parametrize("long_result", [False, True])
def test_sensitive_success_replay_is_safe(source, audit, long_result) -> None:
    secret = "receipt-test-secret"
    calls = []
    output = {"headers": {"authorization": secret}, "padding": "x" * 2000}
    if not long_result:
        output["padding"] = "public"

    def apply() -> dict:
        """返回含敏感字段的结果。"""
        calls.append(1)
        return output

    class Sandbox:
        async def execute(self, code, *, inputs, timeout):
            return SandboxResult(ok=True, output=apply())

    spec = build_spec(apply)
    spec.governance = ToolGovernance(
        sensitive=() if audit else ("headers.authorization",),
        audit_sensitive=audit,
        idempotency_key=lambda args, subject: "safe-key",
    )
    specs = [spec]
    name = spec.name
    if source == "code":
        spec.source, spec.handler, spec.code = "code", None, "unused by fake sandbox"
    elif source == "composite":
        flow = build_composite_spec("flow", "流程", [{"tool": spec.name}])
        flow.governance = replace(spec.governance, sensitive=() if audit else ("steps",))
        specs.append(flow)
        name = flow.name
    executor = _executor(specs)
    executor.sandbox = Sandbox()
    executor.result_char_limit = 160 if long_result else None

    first = asyncio.run(executor.execute(name, {}, call_id="c1"))
    second = asyncio.run(executor.execute(name, {}, call_id="c2"))
    _assert_replay(first, second, calls, ok=True, status="succeeded")
    assert secret in first[2], "脱敏只改事件副本，不能改模型正文"
    for events in (first[0], second[0]):
        assert secret not in json.dumps([e.data for e in events], ensure_ascii=False)
    if long_result:
        with pytest.raises(json.JSONDecodeError):
            json.loads(second[2])
        if not audit:
            assert "预览不可用" in second[0][-1].data["preview"]
    elif not audit:
        assert "redacted" in second[0][-1].data["preview"]


@pytest.mark.parametrize(
    "source", ["native", "code", "code_exception", "composite", "composite_partial"]
)
@pytest.mark.parametrize("audit", [False, True])
def test_sensitive_failure_replay_and_receipt_are_safe(source, audit) -> None:
    secret = "receipt-error-secret"
    calls = []

    def apply() -> dict:
        """失败信息也可能含敏感结果。"""
        calls.append(1)
        raise RuntimeError(secret)

    class Sandbox:
        async def execute(self, code, *, inputs, timeout):
            calls.append(1)
            if source == "code_exception":
                raise RuntimeError(secret)
            return SandboxResult(ok=False, error=secret)

    spec = build_spec(apply)
    spec.governance = ToolGovernance(
        sensitive=() if audit else ("headers.authorization",),
        audit_sensitive=audit,
        idempotency_key=lambda args, subject: "safe-key",
    )
    specs = [spec]
    name = spec.name
    if source.startswith("code"):
        spec.source, spec.handler, spec.code = "code", None, "unused by fake sandbox"
    elif source.startswith("composite"):
        steps = [{"tool": spec.name}]
        if source == "composite_partial":
            def completed() -> dict:
                """已经完成的前一步。"""
                return {"ok": True}

            specs.append(build_spec(completed))
            steps.insert(0, {"tool": "completed"})
        flow = build_composite_spec("flow", "流程", steps)
        flow.governance = spec.governance
        specs.append(flow)
        name = flow.name
    executor = _executor(specs)
    executor.sandbox = Sandbox()

    first = asyncio.run(executor.execute(name, {}, call_id="c1"))
    second = asyncio.run(executor.execute(name, {}, call_id="c2"))
    status = {"code": "failed", "composite_partial": "partial"}.get(source, "unknown")
    _assert_replay(first, second, calls, ok=False, status=status)
    assert secret in first[2]
    for events in (first[0], second[0]):
        assert secret not in json.dumps([e.data for e in events], ensure_ascii=False)
    assert "敏感错误已折叠" in _receipt(second[0])["error"]


@pytest.mark.parametrize("protection", ["audit", "paths", "added_path"])
@pytest.mark.parametrize("long_result", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_replay_uses_current_stricter_governance(protection, long_result, failure) -> None:
    secret = "newly-sensitive-secret"
    calls = []

    def apply() -> dict:
        """治理收紧后不能重放旧的明文预览。"""
        calls.append(1)
        if failure:
            raise RuntimeError(secret)
        return {"token": secret, "padding": "x" * (2000 if long_result else 2)}

    spec = build_spec(apply)
    spec.governance = ToolGovernance(
        sensitive=("padding",) if protection == "added_path" else (),
        idempotency_key=lambda args, subject: "safe-key",
    )
    executor = _executor([spec])
    executor.result_char_limit = 160 if long_result else None
    first = asyncio.run(executor.execute(spec.name, {}, call_id="c1"))
    spec.governance = replace(
        spec.governance,
        audit_sensitive=protection == "audit",
        sensitive=spec.governance.sensitive + (("token",) if protection != "audit" else ()),
    )
    second = asyncio.run(executor.execute(spec.name, {}, call_id="c2"))
    assert calls == [1]
    assert first[1:] == second[1:]
    assert secret in second[2]
    assert secret not in json.dumps([e.data for e in second[0]], ensure_ascii=False)
    before, after = _receipt(first[0]), _receipt(second[0])
    for field in ("status", "retryable", "idempotency_key"):
        assert before[field] == after[field]
    assert after["call_id"] == "c2" and after["replayed"] is True


class _DependencyPolicy:
    yai_subject_aware = True

    def __init__(self):
        self.deny = lambda name, args, subject: False
        self.seen = []

    async def check(self, name, arguments, *, subject=None):
        self.seen.append((name, json.loads(json.dumps(arguments)), subject))
        if self.deny(name, arguments, subject):
            return PermissionDecision.DENY
        return PermissionDecision.ASK


def _assert_dependency_denied(result, calls, expected_calls, secret="dependency-secret"):
    events, ok, text = result
    assert not ok and "重放" in text
    assert calls == expected_calls
    assert secret not in text
    assert secret not in json.dumps([e.data for e in events], ensure_ascii=False)
    assert not any(e.type in (EventType.TOOL_CALL, EventType.PERMISSION_ASKED) for e in events)
    assert _receipt(events)["status"] == "not_started"
    assert _receipt(events)["replayed"] is False
    assert events[-1].data["replay_denied"] is True


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("prewarm", [False, True])
@pytest.mark.parametrize(
    "revoke", ["policy", "subject", "gate", "schema", "unregister", "replace"]
)
def test_composite_replay_rechecks_real_dependencies(nested, prewarm, revoke) -> None:
    calls = []

    def read_secret(amount: int) -> dict:
        """读取敏感数据。"""
        calls.append(1)
        return {"token": "dependency-secret"}

    leaf = build_spec(read_secret)
    leaf.governance = ToolGovernance(
        audit_sensitive=True, idempotency_key=lambda args, subject: "leaf-key"
    )
    inner = build_composite_spec(
        "inner", "内层", [{"tool": leaf.name, "args": {"amount": 7}}]
    )
    inner.governance = ToolGovernance(
        audit_sensitive=True, idempotency_key=lambda args, subject: "inner-key"
    )
    flow = build_composite_spec(
        "flow", "外层", [{"tool": inner.name}] if nested else inner.steps
    )
    flow.governance = inner.governance
    executor = _executor([leaf, inner, flow])
    policy = _DependencyPolicy()
    executor.policy = policy
    if prewarm:
        child_name, args = (inner.name, {}) if nested else (leaf.name, {"amount": 7})
        assert asyncio.run(executor.execute(child_name, args, subject="owner"))[1]
    first = asyncio.run(executor.execute("flow", {}, subject="owner", call_id="c1"))
    assert first[1] and "dependency-secret" in first[2]
    legal = asyncio.run(executor.execute("flow", {}, subject="owner", call_id="c2"))
    _assert_replay(first, legal, calls, ok=True, status="succeeded")
    if revoke == "policy":
        policy.deny = lambda name, args, subject: name == leaf.name and args == {"amount": 7}
    elif revoke == "subject":
        policy.deny = lambda name, args, subject: name == leaf.name and subject == "other"
    elif revoke == "gate":
        leaf.governance = replace(
            leaf.governance,
            gate=lambda args, subject: GateDecision(reject="dependency-secret")
            if args["amount"] == 7 and subject == "other" else None,
        )
    elif revoke == "schema":
        leaf.input_schema["properties"]["amount"]["maximum"] = 5
    else:
        executor.registry.unregister(leaf.name)
        if revoke == "replace":
            executor.registry.register(replace(leaf))
    denied = asyncio.run(executor.execute("flow", {}, subject="other", call_id="c3"))
    _assert_dependency_denied(denied, calls, [1])


def test_dependency_snapshot_uses_resolved_steps_not_redacted_events() -> None:
    calls = []

    def seed() -> dict:
        """产生下一步参数。"""
        calls.append("seed")
        return {"payload": {"token": "dependency-secret"}}

    def consume(payload: dict) -> dict:
        """接收真实参数并修改执行副本。"""
        calls.append("consume")
        payload["token"] = "handler-mutated"
        return {"ok": True}

    seed_spec, leaf = build_spec(seed), build_spec(consume)
    seed_spec.governance = ToolGovernance(audit_sensitive=True)
    leaf.governance = ToolGovernance(sensitive=("payload.token",))
    flow = build_composite_spec("flow", "引用前一步", [
        {"tool": "seed"},
        {"tool": "consume", "args": {"payload": "{{$steps.0.payload}}"}},
    ])
    flow.governance = ToolGovernance(
        audit_sensitive=True, idempotency_key=lambda args, subject: "flow-key"
    )
    executor = _executor([seed_spec, leaf, flow])
    policy = _DependencyPolicy()
    executor.policy = policy
    first = asyncio.run(executor.execute("flow", {}))
    assert first[1]
    assert "dependency-secret" not in json.dumps([e.data for e in first[0]])
    # 之后改写模板和 handler 执行用参数，都不能改变首次已保存的依赖。
    flow.steps[1].args["payload"] = {"token": "template-mutated"}
    policy.deny = lambda name, args, subject: (
        name == "consume" and args["payload"]["token"] == "dependency-secret"
    )
    result = asyncio.run(executor.execute("flow", {}))
    _assert_dependency_denied(result, calls, ["seed", "consume"])
    assert policy.seen[-1][1] == {"payload": {"token": "dependency-secret"}}


@pytest.mark.parametrize("outcome", ["failed", "partial", "timeout", "truncated"])
@pytest.mark.parametrize("revoke_target", ["completed", "last"])
@pytest.mark.parametrize("nested", [False, True])
def test_failed_and_bounded_composites_keep_dependencies(outcome, revoke_target, nested) -> None:
    calls = []

    def completed() -> dict:
        """已完成的调用。"""
        calls.append("completed")
        return {"token": "dependency-secret"}

    async def last() -> dict:
        """最后一步。"""
        calls.append("last")
        if outcome == "timeout":
            await asyncio.Event().wait()
        if outcome in ("failed", "partial"):
            raise RuntimeError("dependency-secret")
        return {"token": "dependency-secret", "padding": "x" * 3000}

    specs = [build_spec(completed), build_spec(last)]
    steps = [] if outcome == "failed" else [{"tool": "completed"}]
    steps.append({"tool": "last"})
    if nested:
        specs.append(build_composite_spec("inner", "内层", steps))
        steps = [{"tool": "inner"}]
    flow = build_composite_spec("flow", "失败或长结果", steps)
    flow.governance = ToolGovernance(
        audit_sensitive=True, idempotency_key=lambda args, subject: "flow-key",
        timeout_ms=20 if outcome == "timeout" else None,
    )
    executor = _executor([*specs, flow])
    if outcome == "truncated":
        executor.result_char_limit = 160
    policy = _DependencyPolicy()
    executor.policy = policy
    first = asyncio.run(executor.execute("flow", {}))
    legal = asyncio.run(executor.execute("flow", {}))
    assert first[1:] == legal[1:]
    assert _receipt(legal[0])["replayed"] is True
    assert _receipt(first[0])["status"] == {
        "failed": "unknown", "partial": "partial", "timeout": "unknown",
        "truncated": "succeeded",
    }[outcome]
    if outcome == "truncated":
        with pytest.raises(json.JSONDecodeError):
            json.loads(first[2])
    expected = ["last"] if outcome == "failed" else ["completed", "last"]
    target = "last" if outcome == "failed" else revoke_target
    policy.deny = lambda name, args, subject: name == target
    denied = asyncio.run(executor.execute("flow", {}))
    _assert_dependency_denied(denied, calls, expected)


@pytest.mark.parametrize("revoke", ["policy", "gate", "unregister", "replace"])
def test_nested_composite_identity_and_governance_are_dependencies(revoke) -> None:
    calls = []

    def leaf() -> dict:
        """敏感叶子。"""
        calls.append(1)
        return {"token": "dependency-secret"}

    inner = build_composite_spec("inner", "内层", [{"tool": "leaf"}])
    outer = build_composite_spec("outer", "外层", [{"tool": "inner"}])
    outer.governance = ToolGovernance(idempotency_key=lambda args, subject: "key")
    executor = _executor([build_spec(leaf), inner, outer])
    policy = _DependencyPolicy()
    executor.policy = policy
    assert asyncio.run(executor.execute("outer", {}))[1]
    if revoke == "policy":
        policy.deny = lambda name, args, subject: name == "inner"
    elif revoke == "gate":
        inner.governance = ToolGovernance(gate=lambda args, subject: GateDecision(reject="no"))
    else:
        executor.registry.unregister("inner")
        if revoke == "replace":
            executor.registry.register(replace(inner))
    _assert_dependency_denied(asyncio.run(executor.execute("outer", {})), calls, [1])


def test_replay_keeps_each_call_snapshot_and_does_not_repeat_approval_hooks() -> None:
    calls = []

    def leaf(payload: dict) -> dict:
        """同一个工具的两个不同调用。"""
        calls.append(payload["amount"])
        return {"token": "dependency-secret"}

    spec = build_spec(leaf)
    flow = build_composite_spec("flow", "多个参数快照", [
        {"tool": "leaf", "args": {"payload": {"amount": 1}}},
        {"tool": "leaf", "args": {"payload": {"amount": 9}}},
    ])
    flow.governance = ToolGovernance(idempotency_key=lambda args, subject: "key")
    executor = _executor([spec, flow])
    policy = _DependencyPolicy()
    executor.policy = policy
    first = asyncio.run(executor.execute("flow", {}))
    assert first[1]

    def unexpected(*args, **kwargs):
        pytest.fail("重放不应运行预览、子幂等键或审批确认")

    def gate(args, subject):
        args["payload"]["amount"] = 0
        return GateDecision(require_approval=True)

    spec.governance = ToolGovernance(
        gate=gate, approval=ApprovalPolicy(required=True),
        preview=unexpected, idempotency_key=unexpected,
    )
    flow.governance = replace(flow.governance, preview=unexpected)
    executor.channel.confirm = unexpected
    for _ in range(2):
        policy.seen.clear()
        legal = asyncio.run(executor.execute("flow", {}))
        assert first[1:] == legal[1:]
        assert _receipt(legal[0])["replayed"] is True
        assert [args for name, args, _ in policy.seen if name == "leaf"] == [
            {"payload": {"amount": 1}}, {"payload": {"amount": 9}},
        ]
    policy.deny = lambda name, args, subject: (
        name == "leaf" and args["payload"]["amount"] == 9
    )
    _assert_dependency_denied(asyncio.run(executor.execute("flow", {})), calls, [1, 9])


def test_dependency_gate_exception_fails_closed_without_sensitive_error() -> None:
    calls = []

    def leaf() -> dict:
        """敏感读取。"""
        calls.append(1)
        return {"token": "dependency-secret"}

    spec = build_spec(leaf)
    flow = build_composite_spec("flow", "流程", [{"tool": "leaf"}])
    flow.governance = ToolGovernance(idempotency_key=lambda args, subject: "key")
    executor = _executor([spec, flow])
    assert asyncio.run(executor.execute("flow", {}))[1]

    def broken_gate(args, subject):
        raise RuntimeError("dependency-secret")

    spec.governance = ToolGovernance(gate=broken_gate)
    _assert_dependency_denied(asyncio.run(executor.execute("flow", {})), calls, [1])


def test_receipt_chain_links_and_detects_tampering() -> None:
    """同一执行器产出的收据串成哈希链；改内容 / 换序 / 抽条都会让校验失败。"""

    def apply(value: str = "") -> dict:
        """写入。"""
        return {"value": value}

    executor = _executor([build_spec(apply)])
    first_events, _, _ = asyncio.run(
        executor.execute("apply", {"value": "a"}, call_id="c1")
    )
    second_events, _, _ = asyncio.run(
        executor.execute("apply", {"value": "b"}, call_id="c2")
    )
    third_events, _, _ = asyncio.run(
        executor.execute("apply", {"value": "c"}, call_id="c3")
    )
    first, second, third = (
        _receipt(first_events),
        _receipt(second_events),
        _receipt(third_events),
    )

    assert first["prev_hash"] == ""
    assert second["prev_hash"] == first["receipt_hash"]
    assert third["prev_hash"] == second["receipt_hash"]
    assert verify_receipt_chain([first, second, third]) is True
    assert verify_receipt_chain([]) is True

    tampered = dict(second, status="failed")
    assert verify_receipt_chain([first, tampered, third]) is False
    assert verify_receipt_chain([second, first, third]) is False
    assert verify_receipt_chain([first, third]) is False
    assert verify_receipt_chain([dict(first, receipt_hash="")]) is False
