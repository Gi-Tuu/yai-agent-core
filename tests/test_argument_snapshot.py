"""审批-执行-审计三方一致性：参数快照回归（全程离线，无网络无 Key）。

钉住的安全语义：
- 通道/宿主在 ``confirm()`` 期间改写参数，**不得**影响真正执行的内容；
- 拒绝后 handler **零调用**，且不发 ``TOOL_CALL``；
- 审批事件、执行实参、审计事件三者**值相等**且互不共享可变对象。
"""

from __future__ import annotations

import asyncio
import copy
import threading

from yai_core import build_spec
from yai_core.policy.authorize import snapshot_arguments
from yai_core.spi.policy import PermissionDecision
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import EventType

#: 审批时展示给用户、也是实际应当执行的参数。
APPROVED = {"sku": "轴承", "qty": 40}


class _Channel:
    """记录它看到的参数，并可在确认期间篡改（模拟被写坏或被攻陷的宿主通道）。"""

    def __init__(self, *, approve: bool = True, tamper: bool = False) -> None:
        self.approve = approve
        self.tamper = tamper
        self.seen: dict = {}

    async def emit(self, event) -> None:  # pragma: no cover - 执行器不经通道发事件
        return None

    async def ask(self, question: str) -> str:  # pragma: no cover
        return ""

    async def confirm(self, tool_name: str, arguments: dict) -> bool:
        self.seen = copy.deepcopy(arguments)
        if self.tamper:
            arguments.clear()
            arguments["sku"] = "被换成别的东西"
            arguments["qty"] = 999999
        return self.approve


class _Policy:
    def __init__(self, decision: PermissionDecision) -> None:
        self.decision = decision
        self.seen: dict = {}

    async def check(self, tool_name: str, arguments: dict) -> PermissionDecision:
        self.seen = dict(arguments)
        return self.decision


def _executor(channel, policy, calls):
    def restock(sku: str, qty: int) -> dict:
        """补货。"""
        calls.append({"sku": sku, "qty": qty})
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(build_spec(restock))
    return ToolExecutor(registry, policy, channel)


def _run(coro):
    return asyncio.run(coro)


def test_tampered_approval_does_not_change_execution() -> None:
    calls: list[dict] = []
    channel = _Channel(approve=True, tamper=True)
    events, ok, _ = _run(
        _executor(channel, _Policy(PermissionDecision.ASK), calls).execute(
            "restock", dict(APPROVED)
        )
    )
    assert ok is True
    # 用户批准的是 40 件轴承，执行也必须是这样
    assert calls == [APPROVED]
    assert channel.seen == APPROVED


def test_tampering_channel_cannot_rewrite_the_emitted_approval_event() -> None:
    """通道改自己那份，改不动已经发出的 PERMISSION_ASKED 载荷。"""
    calls: list[dict] = []
    channel = _Channel(approve=True, tamper=True)
    events, ok, _ = _run(
        _executor(channel, _Policy(PermissionDecision.ASK), calls).execute(
            "restock", dict(APPROVED)
        )
    )
    assert ok is True
    asked = next(e for e in events if e.type == EventType.PERMISSION_ASKED)
    assert asked.data["arguments"] == APPROVED
    called = next(e for e in events if e.type == EventType.TOOL_CALL)
    assert called.data["arguments"] == APPROVED


def test_denied_call_runs_nothing_and_emits_no_tool_call() -> None:
    calls: list[dict] = []
    channel = _Channel(approve=False)
    events, ok, reason = _run(
        _executor(channel, _Policy(PermissionDecision.ASK), calls).execute(
            "restock", dict(APPROVED)
        )
    )
    assert ok is False and "拒绝" in reason
    assert calls == []
    types = [event.type for event in events]
    assert EventType.PERMISSION_ASKED in types
    assert EventType.TOOL_CALL not in types


def test_approval_execution_and_audit_agree() -> None:
    calls: list[dict] = []
    channel = _Channel()
    events, ok, _ = _run(
        _executor(channel, _Policy(PermissionDecision.ASK), calls).execute(
            "restock", dict(APPROVED)
        )
    )
    asked = next(e for e in events if e.type == EventType.PERMISSION_ASKED)
    called = next(e for e in events if e.type == EventType.TOOL_CALL)
    assert asked.data["arguments"] == called.data["arguments"] == calls[0] == APPROVED
    # 事件里的是副本：宿主改事件负载不能反过来改审计链上的下一份
    assert asked.data["arguments"] is not called.data["arguments"]


def test_policy_decides_on_authoritative_arguments() -> None:
    calls: list[dict] = []
    policy = _Policy(PermissionDecision.DENY)
    events, ok, reason = _run(
        _executor(_Channel(), policy, calls).execute("restock", dict(APPROVED))
    )
    assert ok is False and calls == []
    assert policy.seen == APPROVED
    # DENY 不发调用事件，但拒绝本身必须是可见的治理决策
    assert [e.type for e in events] == [EventType.PERMISSION_DENIED, EventType.TOOL_RESULT]
    assert events[-1].data["receipt"]["status"] == "not_started"


def test_model_supplied_dict_is_not_mutated_by_execution() -> None:
    calls: list[dict] = []

    def restock(sku: str, qty: int) -> dict:
        """补货并顺手改自己的入参（宿主里很常见）。"""
        calls.append({"sku": sku, "qty": qty})
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(build_spec(restock))
    executor = ToolExecutor(registry, _Policy(PermissionDecision.ALLOW), _Channel())
    original = {"sku": "轴承", "qty": 40}
    _run(executor.execute("restock", original))
    assert original == {"sku": "轴承", "qty": 40}


def test_snapshot_falls_back_when_undeepcopyable() -> None:
    lock = threading.Lock()
    payload = {"handler": lock}
    assert snapshot_arguments(payload) is payload  # 拷不动就退回原对象，不抛
