"""组合工具执行：逐步编排内部工具，每一步递归走完整权限链路。

从 ``tools/executor.py`` 拆出；执行器方法只做薄委托。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from yai_core.kernel.budget import BudgetExceeded
from yai_core.policy.authorize import (
        approval_context,
        confirm_with_context,
        fill_prompt,
)
from yai_core.policy.redact import redact_arguments
from yai_core.tools.composer import resolve_args
from yai_core.tools.execution_audit import event_preview
from yai_core.tools.result import _composite_timeout_message
from yai_core.types import AgentEvent, ChangePreview, EventType, GateDecision, ToolSpec


async def execute_composite(
    executor: Any,
    spec: ToolSpec,
    arguments: dict[str, Any],
    subject: Any = None,
    decision: GateDecision | None = None,
    *,
    budget: Any = None,
    events: list[AgentEvent] | None = None,
    preview: ChangePreview | None = None,
    call_id: str = "",
    trace: list[Any],
    progress: dict[str, Any] | None = None,
) -> tuple[list[AgentEvent], bool, str]:
        """执行组合工具：逐步编排内部工具，每一步都递归走完整权限链路。

        组合工具本身不产生新原始能力，因此不再对组合名做一次权限检查
        （那会让用户看到一个黑盒名字）；权限精确落到每一步的内部工具上。

        例外：**组合工具自己声明了审批要求**（``gate`` 或 ``approval`` 抬出来的
        ``require_approval``）。那是宿主写明"这整条工作流开工前要问一次"，
        不是黑盒——所以这一步真的去问，绝不静默丢掉这份保护。问的时候只走
        :meth:`Channel.confirm`，不再跑一遍策略（策略对黑盒名字的判断仍然不做）。
        """
        # 由调用方传入同一个事件列表：工作流超时被取消时，已执行步骤的事件
        # 必须仍然可见——副作用发生了，审计链不能只剩一句"超时"。
        events = [] if events is None else events
        steps = spec.steps
        if not steps:
            return events, False, f"组合工具 {spec.name} 缺少步骤"
        # 声明超时不仅要包住整条工作流，还要下发到每个步骤：同步 handler 默认在
        # 事件循环线程里直接跑，外包的超时在它返回前根本没机会触发。
        declared = spec.governance.timeout_ms
        if declared is None:
            deadline = None
        else:
            deadline = asyncio.get_running_loop().time() + declared / 1000.0
        step_names = [step.tool for step in steps]
        # 对外副本按**组合工具自己的声明**脱敏（与 authorize 同一口径）：
        # 宿主在清单上写了 sensitive，就不能因为这条路走了组合分支而失效。
        # 执行用的 arguments 保持真值，脱敏只影响"别人看见什么"。
        shown = redact_arguments(
            arguments,
            spec.governance.sensitive,
            broad=spec.governance.audit_sensitive,
        )
        if decision is not None and decision.require_approval:
            asked = redact_arguments(arguments, spec.governance.sensitive)
            payload: dict[str, Any] = {
                "tool": spec.name,
                "arguments": asked,
                "steps": step_names,
                "approval_required": True,
                "reason": decision.reason,
            }
            if decision.prompt:
                payload["prompt"] = fill_prompt(decision.prompt, asked)
            for key, value in (
                ("risk", decision.risk),
                ("scope", decision.scope),
                ("target", decision.target),
            ):
                if value:
                    payload[key] = value
            preview_data = preview.to_dict() if preview is not None else None
            if preview_data:
                payload["preview"] = preview_data
            events.append(AgentEvent(EventType.PERMISSION_ASKED, payload))
            # 与 authorize_tool_call 同一个口径：通道声明了旗标才递第三个参数。
            shown_for_channel = redact_arguments(arguments, spec.governance.sensitive)
            context = approval_context(
                spec.name,
                require_approval=True,
                reason=decision.reason,
                prompt=payload.get("prompt", ""),
                risk=decision.risk,
                scope=decision.scope,
                target=decision.target,
                preview=preview_data,
            )
            approved = await confirm_with_context(
                executor.channel, spec.name, shown_for_channel, context
            )
            if not approved:
                message = f"用户/宿主拒绝执行工具 {spec.name}"
                events.append(
                    executor._audit.denied_event(
                        spec, spec.name, arguments, message, "approval"
                    )
                )
                return events, False, message
            if preview is not None:
                preview_failure, flags = await executor._approved_preview_failure(
                    spec, arguments, subject, preview
                )
                if preview_failure is not None:
                    events.append(AgentEvent(
                        EventType.TOOL_RESULT,
                        {"tool": spec.name, "ok": False, "error": preview_failure, **flags},
                    ))
                    return events, False, preview_failure
        events.append(
            AgentEvent(
                EventType.TOOL_COMPOSED,
                {"tool": spec.name, "steps": step_names, "arguments": shown},
            )
        )

        step_results: list[Any] = []
        context = {"input": arguments, "steps": step_results}
        for index, step in enumerate(steps):
            try:
                resolved = resolve_args(step.args, context)
            except Exception as exc:  # noqa: BLE001 - 编排错误要回灌而非崩溃
                events.append(
                    AgentEvent(
                        EventType.TOOL_RESULT,
                        {"tool": spec.name, "ok": False,
                         "error": f"第 {index + 1} 步参数解析失败: {exc}"},
                    )
                )
                return events, False, f"组合工具 {spec.name} 第 {index + 1} 步参数解析失败: {exc}"

            step_call_id = f"{call_id}:{index}" if call_id else ""
            step_timeout = None
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    assert declared is not None
                    message = _composite_timeout_message(spec.name, declared)
                    events.append(
                        AgentEvent(
                            EventType.TOOL_RESULT,
                            {
                                "tool": spec.name,
                                "ok": False,
                                "error": message,
                                "timed_out": True,
                            },
                        )
                    )
                    return events, False, message
                step_timeout = remaining
            if progress is not None:
                step_spec = (
                    executor.registry.get(step.tool)
                    if executor.registry.has(step.tool)
                    else None
                )
                progress.update(
                    tool=step.tool,
                    index=index,
                    call_id=step_call_id,
                    started=time.monotonic(),
                    arguments=redact_arguments(
                        resolved,
                        step_spec.governance.sensitive if step_spec else (),
                        broad=step_spec.governance.audit_sensitive if step_spec else False,
                    ),
                )
            try:
                sub_events, ok, sub_text = await executor.execute(
                    step.tool,
                    resolved,
                    subject=subject,
                    budget=budget,
                    call_id=step_call_id,
                    _trace=trace,
                    _timeout=step_timeout,
                )
            except BudgetExceeded as exc:
                # 预算硬熔断要继续冒泡到 AgentLoop 统一收尾，但本组合已经密封的
                # 子收据不能丢：把已积累的事件挂到异常上（组合自身的事件在前，
                # 子步骤的部分事件在后），由 AgentLoop 在熔断收尾时补发，
                # 否则导出的收据链会因缺环而校验失败。
                exc.partial_events = events + list(exc.partial_events)
                raise
            events.extend(sub_events)
            if not ok:
                events.append(
                    AgentEvent(
                        EventType.TOOL_RESULT,
                        {"tool": spec.name, "ok": False,
                         "error": f"第 {index + 1} 步（{step.tool}）失败: {sub_text}"},
                    )
                )
                return (
                    events,
                    False,
                    f"组合工具 {spec.name} 在第 {index + 1} 步（{step.tool}）失败：{sub_text}",
                )
            try:
                step_results.append(json.loads(sub_text))
            except (json.JSONDecodeError, TypeError):
                step_results.append(sub_text)

        final = {"steps": step_results}
        text = json.dumps(final, ensure_ascii=False, default=str)
        text, truncated, result_chars = executor._bound(text)
        events.append(
            AgentEvent(
                EventType.TOOL_RESULT,
                {
                    "tool": spec.name,
                    "ok": True,
                    "preview": event_preview(spec, final, text),
                    "truncated": truncated,
                    "result_chars": result_chars,
                },
            )
        )
        return events, True, text
