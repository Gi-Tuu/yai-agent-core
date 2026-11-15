"""审批预览：生成、失败关闭、审批后 token 复核。

从 ``tools/executor.py`` 拆出；执行器方法只做薄委托。
"""

from __future__ import annotations

from typing import Any

from yai_core.types import AgentEvent, ChangePreview, EventType, ToolSpec


class _PreviewError(RuntimeError):
    """宿主声明的变更预览无法生成；审批不能建立在未知变更上。"""


async def preview_or_reject(
    executor: Any,
    spec: ToolSpec,
    name: str,
    arguments: dict[str, Any],
    subject: Any,
    events: list[AgentEvent],
    started: float,
    call_id: str,
    key: str,
    ) -> tuple[ChangePreview | None, str | None]:
    """生成审批预览；失败时按失败关闭返回拒绝原因。"""
    try:
        return await executor._generate_preview(spec, arguments, subject), None
    except _PreviewError as exc:
        message = str(exc)
        executor._audit.fail(
            events,
            spec=spec,
            name=name,
            call_id=call_id,
            started=started,
            key=key,
            message=message,
            flags={"preview_failed": True},
        )
        return None, message


async def recheck_approved_preview(
    executor: Any,
    spec: ToolSpec,
    name: str,
    arguments: dict[str, Any],
    subject: Any,
    preview: ChangePreview | None,
    auth_events: list[AgentEvent],
    events: list[AgentEvent],
    started: float,
    call_id: str,
    key: str,
    ) -> str | None:
    """审批后复核预览 token：目标或参数变了就不能执行。"""
    if preview is None or not any(
        event.type == EventType.PERMISSION_ASKED for event in auth_events
    ):
        return None
    message, flags = await executor._approved_preview_failure(
        spec, arguments, subject, preview
    )
    if message is not None:
        executor._audit.fail(
            events, spec=spec, name=name, call_id=call_id,
            started=started, key=key, message=message, flags=flags,
        )
    return message


async def approved_preview_failure(
    executor: Any,
    spec: ToolSpec,
    arguments: dict[str, Any],
    subject: Any,
    preview: ChangePreview,
    ) -> tuple[str | None, dict[str, Any]]:
    """只复核并返回原因和标记；事件与收据归调用方所有。"""
    try:
        current_preview = await executor._generate_preview(spec, arguments, subject)
    except _PreviewError as exc:
        return str(exc), {"preview_failed": True}
    assert current_preview is not None
    if current_preview.token != preview.token:
        return (
            "审批所依据的变更预览已失效（确认后目标或参数发生了变化），"
            "本次未执行；请重新发起以查看最新预览并再次确认",
            {"preview_stale": True},
        )
    return None, {}
