"""执行收据、后置条件与拒绝事件的审计旁路。

从 ``ToolExecutor`` 抽出的“执行之后/拒绝之时”的纯旁路逻辑：
它们只决定收据、事件和返回值，不参与工具执行本身的分支。抽出来后
executor 的 ``_execute_impl`` 只负责编排，审计口径集中在这里。

行为约定（与抽取前逐字一致）：
- 每条收据用 ``prev_hash`` 串成链，改字段/抽条/换序可被
  :func:`~yai_core.types.verify_receipt_chain` 发现；
- 后置条件只作用于 ``unknown``，确认发生才改判 ``succeeded``，
  拒绝或检查自身失败保持 ``unknown``；
- ``PERMISSION_DENIED`` 的参数按工具声明的敏感路径脱敏后再进事件。
"""

from __future__ import annotations

import inspect
import json
import time
from dataclasses import replace
from typing import Any

from yai_core.policy.redact import redact_arguments
from yai_core.tools.registry import ToolRegistry
from yai_core.types import (
    AgentEvent,
    EventType,
    ExecutionReceipt,
    ReceiptStatus,
    ToolSpec,
)


def event_error(spec: ToolSpec, text: str) -> str:
    """错误是非结构化文本，声明了敏感治理就整体折叠；模型正文不变。"""
    if text and (spec.governance.audit_sensitive or spec.governance.sensitive):
        return f"[敏感错误已折叠：{len(text)} 字符]"
    return text


class ExecutionAudit:
    """进程内收据链与拒绝/后置条件事件的唯一组装处。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        # 收据哈希链：上一条的 receipt_hash，串起本次进程内的执行审计链。
        self._last_receipt_hash = ""

    def receipt(
        self,
        *,
        call_id: str,
        tool: str,
        status: ReceiptStatus,
        started: float,
        key: str = "",
        error: str = "",
        replayed: bool = False,
    ) -> ExecutionReceipt:
        # 导出字段必须在密封前脱敏，否则导出副本将无法通过链校验。
        if self.registry.has(tool):
            error = event_error(self.registry.get(tool), error)
        receipt = ExecutionReceipt(
            call_id=call_id,
            tool=tool,
            status=status,
            # 只有宿主给出稳定 key 时，重试才有去重保护。
            retryable=bool(key) and status in ("failed", "unknown", "partial"),
            idempotency_key=key,
            error=error,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            replayed=replayed,
        ).seal(self._last_receipt_hash)
        # 信封串成链：宿主导出收据后用 verify_receipt_chain() 即可发现任何改动。
        self._last_receipt_hash = receipt.receipt_hash
        return receipt

    def attach(self, events: list[AgentEvent], receipt: ExecutionReceipt) -> None:
        # 代码工具可能刚因 TTL 过期被注销；其固定退役提示不需要治理脱敏。
        spec = (
            self.registry.get(receipt.tool)
            if self.registry.has(receipt.tool)
            else None
        )
        data = receipt.to_dict()
        for event in reversed(events):
            if (
                event.type == EventType.TOOL_RESULT
                and event.data.get("tool") == receipt.tool
                and "receipt" not in event.data
            ):
                if spec is not None and "error" in event.data:
                    event.data["error"] = event_error(spec, event.data["error"])
                event.data["receipt"] = data
                return
        events.append(
            AgentEvent(
                EventType.TOOL_RESULT,
                {
                    "tool": receipt.tool,
                    "ok": receipt.status == "succeeded",
                    "error": data["error"],
                    "receipt": data,
                },
            )
        )

    def denied_event(
        self,
        spec: ToolSpec,
        name: str,
        arguments: dict[str, Any],
        reason: str,
        source: str,
    ) -> AgentEvent:
        """构造一条 ``PERMISSION_DENIED``：拒绝也是一次治理决策，必须可见。"""
        return AgentEvent(
            EventType.PERMISSION_DENIED,
            {
                "tool": name,
                "source": source,
                "reason": event_error(spec, reason),
                "arguments": redact_arguments(
                    arguments, spec.governance.sensitive,
                    broad=spec.governance.audit_sensitive,
                ),
            },
        )

    def fail(
        self,
        events: list[AgentEvent],
        *,
        spec: ToolSpec | None,
        name: str,
        call_id: str,
        started: float,
        message: str,
        status: ReceiptStatus = "not_started",
        key: str = "",
        flags: dict[str, Any] | None = None,
    ) -> tuple[list[AgentEvent], bool, str]:
        """追加失败结果与收据；拒绝/校验/幂等/预览路径共用这一种形状。"""
        payload: dict[str, Any] = {"tool": name, "ok": False, "error": message}
        if flags:
            payload.update(flags)
        events.append(AgentEvent(EventType.TOOL_RESULT, payload))
        self.attach(
            events,
            self.receipt(
                call_id=call_id,
                tool=name,
                status=status,
                started=started,
                key=key,
                error=message,
            ),
        )
        return events, False, message

    async def apply_postcondition(
        self,
        name: str,
        arguments: dict[str, Any],
        events: list[AgentEvent],
        ok: bool,
        text: str,
    ) -> tuple[bool, str]:
        """在 ``unknown`` 结果上跑宿主的后置条件检查，决定是否把收据改判为 succeeded。"""
        spec = self.registry.get(name) if self.registry.has(name) else None
        if spec is None or spec.governance is None:
            return ok, text
        check = spec.governance.postcondition
        if check is None:
            return ok, text
        final = next(
            (e for e in reversed(events) if e.type == EventType.TOOL_RESULT), None
        )
        if final is None or not isinstance(final.data.get("receipt"), dict):
            return ok, text
        try:
            receipt = ExecutionReceipt.from_dict(final.data["receipt"])
        except (TypeError, ValueError):
            return ok, text
        if receipt.status != "unknown":
            return ok, text

        try:
            outcome = check(dict(arguments))
            if inspect.isawaitable(outcome):
                outcome = await outcome
            confirmed = bool(outcome)
            verdict = "confirmed" if confirmed else "rejected"
        except Exception:  # noqa: BLE001 - 检查失败只能保持 unknown，不能替宿主猜
            confirmed = False
            verdict = "error"

        if confirmed:
            updated = replace(
                receipt,
                status="succeeded",
                retryable=False,
                error="",
                postcondition=verdict,
            ).seal(receipt.prev_hash)
        else:
            updated = replace(receipt, postcondition=verdict).seal(receipt.prev_hash)
        # 这条是链尾：重新密封后把链尾指回去，后续收据继续接在它后面。
        self._last_receipt_hash = updated.receipt_hash
        final.data["receipt"] = updated.to_dict()
        final.data["postcondition"] = verdict
        if not confirmed:
            return ok, text
        final.data["ok"] = True
        final.data["verified"] = True
        final.data.pop("error", None)
        return True, text + "\n（后置条件已确认副作用发生，收据改判为 succeeded）"


def infer_status(
    events: list[AgentEvent], *, ok: bool, text: str, source: str
) -> ReceiptStatus:
    """从子执行器的现有事件推断收据状态（不改它们的返回签名）。"""
    if ok:
        return "succeeded"
    if any(e.data.get("timed_out") for e in events):
        return "unknown"
    if any(
        e.data.get("preview_stale") or e.data.get("preview_failed")
        for e in events
    ):
        return "not_started"
    if source == "code":
        if "存活期" in text or "未提供代码沙箱" in text:
            return "not_started"
        if "沙箱调用失败" in text:
            return "unknown"
        return "failed"
    if source == "composite":
        child_statuses = [
            e.data["receipt"].get("status")
            for e in events
            if e.type == EventType.TOOL_RESULT
            and isinstance(e.data.get("receipt"), dict)
        ]
        if "partial" in child_statuses:
            return "partial"
        has_success = any(s == "succeeded" for s in child_statuses)
        has_problem = any(
            s in ("failed", "unknown", "not_started") for s in child_statuses
        )
        if has_success and has_problem:
            return "partial"
        if "unknown" in child_statuses:
            return "unknown"
        has_call = any(e.type == EventType.TOOL_CALL for e in events)
        if not has_call and any(
            e.type == EventType.PERMISSION_ASKED for e in events
        ):
            return "not_started"
        if child_statuses and all(s == "not_started" for s in child_statuses):
            return "not_started"
        has_successful_result = any(
            e.type == EventType.TOOL_RESULT and e.data.get("ok") is True
            for e in events
        )
        return "partial" if has_successful_result else "failed"
    return "failed"


def event_preview(spec: ToolSpec, payload: Any, text: str) -> str:
    """给 ``TOOL_RESULT`` 事件用的预览：按治理声明脱敏后的前 200 字符。"""
    governance = spec.governance
    if governance.audit_sensitive:
        return f"[敏感结果已折叠：{len(text)} 字符]"
    if governance.sensitive and isinstance(payload, dict):
        try:
            redacted = redact_arguments(payload, governance.sensitive)
            return json.dumps(redacted, ensure_ascii=False, default=str)[:200]
        except Exception:  # noqa: BLE001 - 脱敏失败不能反噬主流程
            return f"[结果预览不可用：{len(text)} 字符]"
    return text[:200]


def replay_preview(spec: ToolSpec, text: str) -> str:
    """重放也按当前治理脱敏；缓存正文可能已截断，不能把解析失败当作明文许可。"""
    payload = None
    if spec.governance.sensitive and not spec.governance.audit_sensitive:
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError):
            return f"[结果预览不可用：{len(text)} 字符]"
        if not isinstance(payload, dict):
            return f"[结果预览不可用：{len(text)} 字符]"
    return event_preview(spec, payload, text)
