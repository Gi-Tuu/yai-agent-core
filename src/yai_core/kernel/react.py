"""ReAct 迭代：模型调用 ↔ 工具执行的主循环。

从 ``kernel/loop.py`` 拆出；``AgentLoop._react_cycle`` 只做薄委托。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any

from yai_core.diagnostics import DiagnosticKind, DiagnosticLevel
from yai_core.kernel.budget import BudgetExceeded
from yai_core.kernel.context import Context
from yai_core.kernel.protocol import (
    _RAW_TOOL_CALL_NOTICE,
    _is_model_stage,
    _looks_like_raw_tool_call,
    _set_outcome,
)
from yai_core.kernel.tiering import STANDARD, TierPolicy
from yai_core.types import AgentEvent, ChatMessage, EventType, Strategy, Subject


@dataclass
class ReactState:
    """一次 ReAct 周期内跨轮累积的状态。"""

    had_failure: bool = False
    last_tier: str | None = None
    terminal_messages: list[str] | None = None
    replanned: list[int] | None = None

    def __post_init__(self) -> None:
        if self.terminal_messages is None:
            self.terminal_messages = []
        if self.replanned is None:
            self.replanned = [0]


async def run_react_cycle(
    loop: Any,
    ctx: Context,
    *,
    use_tools: bool,
    task: str = "",
    strategy: Strategy = Strategy.REACT,
    tier_policy: TierPolicy | None = None,
    tier_counts: dict[str, int] | None = None,
    subject: Subject | None = None,
    outcome: dict[str, object] | None = None,
) -> AsyncIterator[AgentEvent]:
    """核心 ReAct 循环：模型调用 ↔ 工具执行，直到模型不再调工具或触达迭代上限。

    每轮先做上下文压缩（若启用）与成本感知选档（档位相对上轮变化才发事件），
    再调用模型；模型不再发起工具调用即产出最终消息并结束。否则顺序执行本轮
    工具——内核 meta-tool 直接处理、普通工具走 :class:`ToolExecutor`——把失败
    观察回灌促其自我修正，并把执行中新注册工具的完整 schema 并入下一轮
    （"发现了就能调到"）。达到 ``max_iters`` 后要求模型基于已有观察收尾。
    ``use_tools=False``（DIRECT）时不注入任何工具。

    ``outcome`` 是调用方传入的结局字典：终止性失败（模型故障 / 协议异常 /
    严格 direct 拒绝 / 审批预览失效 / 限尾收尾失败）在这里写入
    ``{"status": "error", "error": ...}``，供 astream 落 DONE.status（CORE-4）。
    """
    if use_tools:
        tools_schema, focused_names, focus_decision = await loop._focus_tool_schemas(
            task, tier_counts
        )
        if focus_decision is not None:
            focus_text = {
                "selected": "工具范围选择：已聚焦。",
                "empty_selection": "工具范围选择：空选择，回退全量。",
                "error": "工具范围选择：聚焦失败，回退全量。",
            }[str(focus_decision["reason"])]
            yield AgentEvent(
                EventType.MODEL_MESSAGE,
                {"text": focus_text, "kind": "schema_focus", "schema_focus": focus_decision},
            )
    else:
        tools_schema, focused_names = None, None
    state = ReactState()

    for iteration in range(loop.max_iters):
        # 摘要压缩：超高水位时把旧轮压成摘要（最近完整轮保留），避免长对话
        # 硬裁剪丢失早期事实；压缩本身可观测，模型失败回退硬裁剪。
        async for compact_ev in loop._compact_before_model(ctx):
            yield compact_ev
        # 成本·延迟感知选档：默认 standard 省钱，失败纠偏/后段卡住才升 strong；
        # 关闭策略时恒 standard（旧行为）。档位相对上一轮变化时发事件，不啰嗦。
        if tier_policy is not None:
            tier_dec = tier_policy.select(
                strategy=strategy,
                iteration=iteration,
                had_failure=state.had_failure,
            )
            tier = tier_dec.tier
            tier_reason = tier_dec.reason
        else:
            tier = STANDARD
            tier_reason = "成本感知关闭，执行轮用标准档"
        if tier != state.last_tier:
            yield AgentEvent(
                EventType.TIER_CHANGED,
                {"tier": tier, "reason": tier_reason, "iteration": iteration},
            )
        state.last_tier = tier
        try:
            messages, state_event = await loop._model_messages(ctx)
            if state_event is not None:
                yield state_event
            resp = await loop._model_chat(
                messages, tools_schema, tier=tier, tier_counts=tier_counts
            )
        except BudgetExceeded:
            # 预算熔断冒泡到 astream 统一收尾，不能被下面的"模型失败体面 return"吞掉。
            raise
        except Exception as exc:  # noqa: BLE001
            is_timeout = isinstance(exc, TimeoutError)
            model_stage = _is_model_stage(exc)
            stage = "model" if model_stage else "loop"
            error_text = f"{type(exc).__name__}: {exc}"
            loop._diag(
                (
                    DiagnosticKind.MODEL_TIMEOUT
                    if is_timeout
                    else DiagnosticKind.MODEL_ERROR
                )
                if model_stage
                else "loop_internal_error",
                (
                    f"执行轮模型调用超过 {loop.model_timeout:g}s 硬超时，已终止"
                    if is_timeout
                    else (
                        f"执行轮模型调用失败（{type(exc).__name__}），已终止本轮"
                        if model_stage
                        else f"执行轮异常（{type(exc).__name__}），已终止本轮"
                    )
                ),
                level=DiagnosticLevel.ERROR,
                stage="react",
                iteration=iteration,
                error=error_text,
            )
            yield AgentEvent(EventType.ERROR, {"stage": stage, "error": error_text})
            _set_outcome(outcome, "error", error_text)
            return

        if resp.tool_calls:
            normalized_calls = loop._normalize_tool_call_ids(
                resp.tool_calls, iteration
            )
            if normalized_calls is None:
                error = (
                    "模型返回了重复的工具调用 id，已拒绝执行以避免重复副作用"
                )
                yield AgentEvent(
                    EventType.ERROR,
                    {"stage": "model_protocol", "error": error},
                )
                _set_outcome(outcome, "error", error)
                return
            if normalized_calls != resp.tool_calls:
                resp = replace(resp, tool_calls=normalized_calls)

        if not resp.tool_calls:
            text = resp.content.strip()
            if _looks_like_raw_tool_call(text):
                # 模型有时会把 <tool_call>/<arg_key> 写进正文，而不是返回
                # 结构化 tool_calls。此时既不能当最终答案展示（用户会看到
                # 一段假调用），也不能尝试解析执行（协议与权限都不可信）。
                yield AgentEvent(
                    EventType.ERROR,
                    {
                        "stage": "model_protocol",
                        "error": _RAW_TOOL_CALL_NOTICE,
                        "raw_tool_markup": True,
                    },
                )
                ctx.add(ChatMessage(role="assistant", content=_RAW_TOOL_CALL_NOTICE))
                yield AgentEvent(
                    EventType.MODEL_MESSAGE,
                    {"text": _RAW_TOOL_CALL_NOTICE, "tier": tier},
                )
                _set_outcome(outcome, "error", _RAW_TOOL_CALL_NOTICE)
                return
            ctx.add(ChatMessage(role="assistant", content=text))
            yield AgentEvent(EventType.MODEL_MESSAGE, {"text": text, "tier": tier})
            return

        if loop.strict_direct and strategy == Strategy.DIRECT:
            # opt-in 的严格模式：direct 本轮没有向模型提供任何工具，模型仍返回
            # tool_calls 时不能执行——否则它等于凭记忆调用了宿主没有授权的工具。
            text = resp.content.strip()
            yield AgentEvent(
                EventType.ERROR,
                {
                    "stage": "direct",
                    "error": "direct 策略下模型返回了工具调用，严格模式已拒绝执行",
                },
            )
            if not text:
                text = "当前策略不允许调用工具，本次没有执行任何工具。"
            ctx.add(ChatMessage(role="assistant", content=text))
            yield AgentEvent(EventType.MODEL_MESSAGE, {"text": text, "tier": tier})
            _set_outcome(
                outcome, "error", "direct 策略下模型返回了工具调用，严格模式已拒绝执行"
            )
            return

        loop._append_assistant_tool_calls(ctx, resp)
        failed: list[str] = []
        newly_registered: list[str] = []
        async for ev in loop._execute_tool_calls(
            resp.tool_calls,
            ctx,
            task,
            failed,
            newly_registered,
            state.replanned,
            tier_policy,
            tier_counts,
            subject,
            terminal_messages=state.terminal_messages,
        ):
            yield ev

        if state.terminal_messages:
            message = state.terminal_messages[0]
            ctx.add(ChatMessage(role="assistant", content=message))
            yield AgentEvent(EventType.MODEL_MESSAGE, {"text": message, "tier": tier})
            _set_outcome(outcome, "error", message)
            return

        # 执行中发现的新工具：下一轮 function-calling 必须能看到其完整 schema，
        # 否则模型"发现了却调不到"。全量模式重算；聚焦模式把新名字并入聚焦集。
        if newly_registered and use_tools:
            tools_schema = loop._schema_after_registration(newly_registered, focused_names)

        if failed:
            # 轻量反思（不额外调用模型）：失败观察回灌成明确引导，让模型自我修正；
            # 同时置失败标记，下一轮选档据此升 strong。
            state.had_failure = True
            loop._append_failure_reflection(ctx, failed)

    # 达到迭代上限：要求模型基于已有观察收尾。
    async for ev in loop._finish_at_iteration_limit(ctx, tier_counts, outcome):
        yield ev
