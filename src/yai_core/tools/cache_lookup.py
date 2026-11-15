"""幂等缓存重放：命中判定、权限复核与 TOOL_RESULT。

从 ``tools/executor.py`` 拆出；执行器方法只做薄委托。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yai_core.policy.authorize import policy_decision
from yai_core.spi import PermissionDecision
from yai_core.tools.execution_audit import event_error, replay_preview
from yai_core.tools.idempotency import _IdempotencyError, _PermissionDependency
from yai_core.tools.result import _fingerprint_arguments
from yai_core.types import AgentEvent, EventType, ToolSpec


@dataclass(frozen=True)
class _CacheLookup:
    """一次幂等查找的结果；hit 非空表示可直接返回 (ok, text)。"""

    key: str
    fingerprint: str
    hit: tuple[bool, str] | None = None


async def lookup_cache(
    executor: Any,
    spec: ToolSpec,
    name: str,
    arguments: dict[str, Any],
    subject: Any,
    events: list[AgentEvent],
    trace: list[_PermissionDependency],
    trace_start: int,
    started: float,
    call_id: str,
) -> _CacheLookup:
        """幂等键生成与缓存重放；命中时直接给出 (ok, text)。"""
        try:
            idempotency_key = await executor._idempotency_key(spec, arguments, subject)
        except _IdempotencyError as exc:
            message = str(exc)
            executor._audit.fail(
                events,
                spec=spec,
                name=name,
                call_id=call_id,
                started=started,
                message=message,
                flags={"receipt_failed": True},
            )
            return _CacheLookup("", "", (False, message))

        fingerprint = _fingerprint_arguments(arguments) if idempotency_key else ""
        cached = executor._idempotency.get(name, idempotency_key, fingerprint)
        if cached is None:
            return _CacheLookup(idempotency_key, fingerprint)

        cached_result, same_arguments = cached
        ok, cached_text, cached_status, cached_error, dependencies = cached_result
        # 子缓存也必须把历史依赖传给父组合，包括父组合稍后超时的路径。
        trace[trace_start:] = dependencies
        if not same_arguments:
            message = (
                f"幂等键 {idempotency_key!r} 已用于另一组参数，拒绝重放；"
                "不同操作请使用不同的幂等键"
            )
            executor._audit.fail(
                events,
                spec=spec,
                name=name,
                call_id=call_id,
                started=started,
                key=idempotency_key,
                message=message,
                flags={"idempotency_conflict": True},
            )
            return _CacheLookup(idempotency_key, fingerprint, (False, message))

        # 重放只交付已有结果，不再弹审批；但当前策略的 DENY 仍然生效——
        # 缓存是去重，不是绕过权限收紧的后门。
        if (
            await policy_decision(executor.policy, name, arguments, subject)
            == PermissionDecision.DENY
        ):
            message = f"权限策略拒绝执行工具 {name}"
            events.append(
                executor._audit.denied_event(spec, name, arguments, message, "policy")
            )
            executor._audit.fail(
                events,
                spec=spec,
                name=name,
                call_id=call_id,
                started=started,
                key=idempotency_key,
                message=message,
                flags={"replay_denied": True},
            )
            return _CacheLookup(idempotency_key, fingerprint, (False, message))
        rejection = await executor._replay_dependency_rejection(dependencies, subject)
        if rejection:
            events.append(executor._audit.denied_event(
                spec, name, arguments, rejection, "replay_dependency"
            ))
            executor._audit.fail(
                events,
                spec=spec,
                name=name,
                call_id=call_id,
                started=started,
                key=idempotency_key,
                message=rejection,
                flags={"replay_denied": True},
            )
            return _CacheLookup(idempotency_key, fingerprint, (False, rejection))

        receipt = executor._audit.receipt(
            call_id=call_id,
            tool=name,
            status=cached_status,
            started=started,
            key=idempotency_key,
            error=cached_error,
            replayed=True,
        )
        payload: dict[str, Any] = {
            "tool": name,
            "ok": ok,
            "replayed": True,
            "receipt": receipt.to_dict(),
        }
        if ok:
            payload["preview"] = replay_preview(spec, cached_text)
            payload["truncated"] = False
            payload["result_chars"] = len(cached_text)
        else:
            payload["error"] = event_error(spec, cached_text)
        events.append(AgentEvent(EventType.TOOL_RESULT, payload))
        return _CacheLookup(idempotency_key, fingerprint, (ok, cached_text))
