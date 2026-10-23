"""可信变更预览：审批人看宿主渲染的差异，执行前复核预览 token 未变。"""

from __future__ import annotations

import asyncio

import pytest

from yai_core import (
    ApprovalPolicy,
    ChangePreview,
    ToolGovernance,
    ToolSpec,
    build_composite_spec,
    build_spec,
)
from yai_core.channels import CollectChannel
from yai_core.policy import AllowlistPolicy
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import EventType, governance_from_manifest, governance_to_manifest


class CaptureChannel(CollectChannel):
    yai_approval_context_aware = True

    def __init__(self, *, approve: bool = True, on_confirm=None) -> None:
        super().__init__()
        self.approve = approve
        self.context: dict | None = None
        self.on_confirm = on_confirm

    async def confirm(self, tool_name: str, arguments: dict, context: dict | None = None) -> bool:
        self.context = context
        if self.on_confirm is not None:
            self.on_confirm()
        return self.approve


def _executor(spec: ToolSpec, channel: CaptureChannel) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(spec)
    return ToolExecutor(registry, AllowlistPolicy((), mode="auto"), channel)


def _preview_spec(state: dict, handler, *, name: str = "apply") -> ToolSpec:
    def preview(arguments, subject):
        return ChangePreview(
            summary="将写入一条记录",
            token=state["token"],
            diff=f"- old\n+ {arguments.get('value', '')}",
            target="notes/a.txt",
        )

    spec = build_spec(handler, name=name)
    spec.governance = ToolGovernance(
        approval=ApprovalPolicy(required=True),
        preview=preview,
    )
    return spec


def _apply(value: str = "") -> dict:
    """写入。"""
    return {"value": value}


def test_preview_is_shown_to_approver_and_executes_when_unchanged() -> None:
    calls: list[str] = []

    def handler(value: str = "") -> dict:
        calls.append(value)
        return {"value": value}

    state = {"token": "rev-1"}
    channel = CaptureChannel()
    spec = _preview_spec(state, handler)
    events, ok, _ = asyncio.run(
        _executor(spec, channel).execute("apply", {"value": "new"})
    )

    assert ok is True and calls == ["new"]
    assert channel.context is not None
    assert channel.context["preview"]["summary"] == "将写入一条记录"
    assert "+ new" in channel.context["preview"]["diff"]
    asked = next(e for e in events if e.type == EventType.PERMISSION_ASKED)
    assert asked.data["preview"]["token"] == "rev-1"
    call = next(e for e in events if e.type == EventType.TOOL_CALL)
    assert call.data["preview"]["target"] == "notes/a.txt"


def test_stale_preview_after_confirmation_blocks_execution() -> None:
    calls: list[str] = []

    def handler(value: str = "") -> dict:
        calls.append(value)
        return {"value": value}

    state = {"token": "rev-1"}
    channel = CaptureChannel(on_confirm=lambda: state.update(token="rev-2"))
    spec = _preview_spec(state, handler)
    events, ok, text = asyncio.run(
        _executor(spec, channel).execute("apply", {"value": "new"})
    )

    assert ok is False
    assert calls == []
    assert "预览已失效" in text
    result = next(e for e in events if e.type == EventType.TOOL_RESULT)
    assert result.data["preview_stale"] is True


def test_preview_provider_may_be_async() -> None:
    async def preview(arguments, subject):
        return {"summary": "异步预览", "token": "v1"}

    def handler() -> dict:
        return {"ok": True}

    spec = build_spec(handler)
    spec.governance = ToolGovernance(
        approval=ApprovalPolicy(required=True), preview=preview
    )
    channel = CaptureChannel()
    _, ok, _ = asyncio.run(_executor(spec, channel).execute("handler", {}))
    assert ok is True
    assert channel.context["preview"]["summary"] == "异步预览"


def test_preview_failure_blocks_before_approval() -> None:
    calls: list[int] = []

    def preview(arguments, subject):
        raise RuntimeError("host state unavailable")

    def handler() -> dict:
        calls.append(1)
        return {"ok": True}

    spec = build_spec(handler)
    spec.governance = ToolGovernance(
        approval=ApprovalPolicy(required=True), preview=preview
    )
    channel = CaptureChannel()
    events, ok, text = asyncio.run(_executor(spec, channel).execute("handler", {}))

    assert ok is False and calls == []
    assert channel.context is None
    assert "变更预览生成失败" in text
    assert next(e for e in events if e.type == EventType.TOOL_RESULT).data[
        "preview_failed"
    ] is True


def test_composite_own_approval_uses_the_same_preview_rule() -> None:
    calls: list[int] = []

    def step() -> dict:
        calls.append(1)
        return {"ok": True}

    state = {"token": "rev-1"}
    channel = CaptureChannel(on_confirm=lambda: state.update(token="rev-2"))

    def preview(arguments, subject):
        return ChangePreview(summary="整条流程", token=state["token"])

    flow = build_composite_spec("flow", "流程", [{"tool": "step"}])
    flow.governance = ToolGovernance(
        approval=ApprovalPolicy(required=True), preview=preview
    )
    registry = ToolRegistry()
    registry.register(build_spec(step))
    registry.register(flow)

    events, ok, text = asyncio.run(
        ToolExecutor(registry, AllowlistPolicy((), mode="auto"), channel).execute(
            "flow", {}
        )
    )

    assert ok is False and calls == []
    assert "预览已失效" in text
    assert any(e.type == EventType.TOOL_RESULT and e.data.get("preview_stale") for e in events)


def test_preview_is_not_serialized_into_manifest() -> None:
    def preview(arguments, subject):
        return ChangePreview(summary="x", token="x")

    governance = ToolGovernance(preview=preview)
    manifest = governance_to_manifest(governance)
    assert "preview" not in manifest
    restored = governance_from_manifest("demo", manifest)
    assert restored.preview is None


def test_change_preview_requires_summary_and_token() -> None:
    with pytest.raises(ValueError):
        ChangePreview(summary="", token="v1")
    with pytest.raises(ValueError):
        ChangePreview(summary="x", token="")
    with pytest.raises(TypeError):
        ChangePreview.from_value("not-a-preview")
