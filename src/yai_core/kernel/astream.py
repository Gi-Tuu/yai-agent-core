"""AgentLoop.astream：路由 → 策略分发 → 收尾的统一入口。

从 ``kernel/loop.py`` 拆出；``AgentLoop.astream`` 只做薄委托。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from yai_core.diagnostics import DiagnosticKind, DiagnosticLevel
from yai_core.kernel.budget import BudgetExceeded
from yai_core.kernel.context import Context
from yai_core.kernel.protocol import _is_model_stage
from yai_core.kernel.tiering import STANDARD, STRONG
from yai_core.types import AgentEvent, ChatMessage, EventType, Strategy, Subject


@dataclass
class AstStreamState:
    """一次 astream 调用的跨阶段状态。"""

    final_text: str = ""
    run_status: str = "ok"
    run_error_text: str | None = None
    outcome: dict[str, object] = field(default_factory=dict)
    tier_counts: dict[str, int] = field(
        default_factory=lambda: {STANDARD: 0, STRONG: 0}
    )


_SYSTEM_TEMPLATE = """你是运行在宿主软件内部的 AI 助手。\
你可以使用宿主提供的工具（见下方工具目录）。\
需要扩展能力时按此选择：\
固定多步编排宿主已有工具（含先取数、再加工）用 compose_tool；\
数据由调用方提供、只需全新纯计算（换算 / 评分 / 统计）用 create_code_tool\
（沙箱内不能回调宿主工具）；\
宿主根本没有该能力，就明确指出缺失处或调用 request_capability。\
不要编造工具不存在的数据。当任务完成时，直接给出可交付的最终结果。

宿主当前提供的能力：
{tools}
"""


# 预算熔断后的固定诚实收尾文案：不再发起任何模型/工具调用，直接给用户可执行的下一步。
_BUDGET_SCOPE_LABELS = {
    "model_calls": "模型调用次数",
    "tool_calls": "工具执行次数",
    "tokens": "Token 用量",
}
_BUDGET_STOP_TEXT = (
    "已达到本次任务设定的{label}预算上限，为避免继续消耗已主动停止，未执行新的调用。"
    "可在宿主侧调高对应预算后重试，或把任务拆成更小的步骤分别执行。"
)


async def run_astream(
    loop: Any,
    task: str,
    *,
    _clarify_depth: int = 0,
    subject: Subject | None = None,
) -> AsyncGenerator[AgentEvent, None]:
    """任务执行的统一入口：路由一次，按策略分发，最后落记忆并发 DONE。

    流程为 ``aclassify`` 得到策略（来源/理由/档位随事件流出，可审计）→
    能力缺口事件与按需发现 → clarify 反问（累积补充后递归重路由，轮数封顶）、
    或 direct/react/plan 执行 → react 与 plan 共用 :meth:`_react_cycle`，
    plan 先经 :meth:`_make_plan` 生成步骤 → 把本轮 user/assistant 写入记忆，
    发带标准/强档调用计数的 DONE。

    ``_clarify_depth`` 只由澄清递归自增，外部调用恒为 0；达上限仍不明确时
    降级为 DIRECT（信息不全不硬调工具），绝不无限反问。
    """
    # 任务级预算：仅顶层（depth==0）新建计数器；澄清递归复用同一个，
    # 防止靠"多反问几轮"重置预算。loop 实例串行复用，无并发竞争。
    loop._current_task = task
    if _clarify_depth == 0:
        loop._active_budget = loop.budget.start() if loop.budget is not None else None
        # 新任务重新计一次宿主状态事件：即使状态与上个任务相同也要发。
        loop._last_state_fingerprint = None
    decision = await loop.router.aclassify(
        task, loop.registry, discoverable=loop._discoverable_catalog()
    )
    strategy = decision.strategy
    # 决策来源（llm/rules）、理由与模型档位随事件流出：每次自适应决策都可审计。
    yield AgentEvent(
        EventType.STRATEGY_SELECTED,
        {
            "strategy": strategy.value,
            "source": decision.source,
            "reason": decision.reason,
            "tier": decision.tier,
        },
    )

    # 能力缺口感知：发缺口事件；配置发现源时把缺口闭环成新工具（本轮即可用）。
    async for ev in loop._emit_capability_gap(decision, task):
        yield ev

    if strategy == Strategy.CLARIFY and _clarify_depth >= loop.max_clarify_rounds:
        # 澄清轮数达上限仍不明确：降级为直接回答（DIRECT，不调工具），由模型说明
        # 信息不足——信息不全时不应硬调工具，这是有意为之；绝不无限反问。
        # 每次策略变化都发事件，tier 沿用路由决策，保证决策可审计。
        strategy = Strategy.DIRECT
        yield AgentEvent(
            EventType.STRATEGY_SELECTED,
            {
                "strategy": Strategy.DIRECT.value,
                "source": "rules",
                "reason": f"已澄清 {_clarify_depth} 轮仍不明确，降级直接回答",
                "tier": decision.tier,
            },
        )

    if strategy == Strategy.CLARIFY:
        # 反问补信息后累积重路由；空回答体面收尾，绝不无限反问。
        async for ev in loop._run_clarify(task, _clarify_depth, subject):
            yield ev
        return

    # 两层工具目录：系统提示只放 name + 一句话摘要（轻量目录），
    # 完整参数结构在 function-calling 的 tools 参数里按需注入，见 _react_cycle。
    ctx = Context(
        _SYSTEM_TEMPLATE.format(tools=loop.registry.catalog_text()),
        # 压缩启用时推迟到模型调用前：先让压缩器看到完整旧轮，再硬裁剪兜底。
        trim_on_add=loop.compaction_policy is None,
    )
    for msg in loop._history_messages():
        ctx.add(msg)
    # 任务起始主动召回：把与本任务相关的长期记忆作为一条 system 上下文注入，
    # 位于历史之后、user 任务之前。clarify 路径已在上方提前 return，不做召回。
    async for recall_event in loop._inject_auto_recall(ctx, task):
        yield recall_event
    ctx.add(ChatMessage(role="user", content=task))

    state = AstStreamState()
    try:
        if strategy == Strategy.PLAN:
            # replan meta-tool 只在多步计划执行中需要，进入 PLAN 时幂等注册，
            # 让模型在计划走不通时显式触发重规划（空宿主 / direct / 简单 react 不注册）。
            loop._ensure_replan_tool()
            async for ev in loop._make_plan(
                ctx,
                task,
                tier=decision.tier,
                tier_policy=loop.tier_policy,
                tier_counts=state.tier_counts,
            ):
                yield ev
            # 计划只是"助手说过的话"，必须再推一把，模型才会进入工具执行；
            # 否则真实模型会把计划本身当成最终答复（离线脚本模型曾掩盖此问题）。
            ctx.add(
                ChatMessage(
                    role="user",
                    content="请按上面的计划逐步调用工具执行，拿到全部结果后给出最终汇报。",
                )
            )

        async for ev in loop._react_cycle(
            ctx,
            use_tools=strategy != Strategy.DIRECT,
            task=task,
            strategy=strategy,
            tier_policy=loop.tier_policy,
            tier_counts=state.tier_counts,
            subject=subject,
            outcome=state.outcome,
        ):
            yield ev
            # 工具范围选择是过程决策，不是答案，也不能写进对话历史。
            if ev.type == EventType.MODEL_MESSAGE and ev.data.get("kind") != "schema_focus":
                state.final_text = ev.data.get("text", state.final_text)
        if state.outcome.get("status") == "error":
            state.run_status = "error"
            state.run_error_text = str(state.outcome.get("error") or "任务执行失败")
    except BudgetExceeded as exc:
        # 执行器在熔断前已密封、还没来得及外发的事件（组合工具的中途子收据）
        # 挂在异常上随行到这里：先补发它们，收据链才不缺环。
        for pending_ev in getattr(exc, "partial_events", None) or []:
            yield pending_ev
        # 预算硬墙：规划/聚焦/执行/收尾任一处触发都在此统一熔断，
        # 不再发起新的模型或工具调用，用固定诚实文案收尾并发可审计事件。
        label = _BUDGET_SCOPE_LABELS.get(exc.scope, exc.scope)
        stop_text = _BUDGET_STOP_TEXT.format(label=label)
        state.final_text = stop_text
        state.run_status = "error"
        state.run_error_text = stop_text
        yield AgentEvent(EventType.MODEL_MESSAGE, {"text": stop_text})
        yield AgentEvent(
            EventType.BUDGET_EXCEEDED,
            {
                "scope": exc.scope,
                "limit": exc.limit,
                "used": exc.used,
                "budget": loop._budget_snapshot(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - 模型超时/失败体面收尾，不穿透到宿主
        # 规划 / 收尾 / 重规划等未被局部捕获的模型异常（含硬超时）在此兜底：
        # 发 ERROR 事件后仍照常落 DONE，宿主拿到有界结果而不是裸异常。
        # react 主轮的模型异常已在循环内局部转 ERROR，不会走到这里。
        # CancelledError 属于 BaseException，不被本分支捕获，从而能干净传播、
        # 真正响应用户终止 / aclose / 任务取消（见 _model_chat）。
        #
        # stage 归因（审计 F5）：只有被 _model_chat 标记过的异常才算 model；
        # 工具治理 / 宿主 SPI / 内核自身的异常一律 loop，不再错报成模型故障。
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
                f"模型调用超过 {loop.model_timeout:g}s 硬超时，已体面收尾"
                if is_timeout
                else (
                    f"模型调用失败（{type(exc).__name__}），已体面收尾"
                    if model_stage
                    else f"内核执行阶段异常（{type(exc).__name__}），已体面收尾"
                )
            ),
            level=DiagnosticLevel.ERROR,
            stage=stage,
            error=error_text,
        )
        state.run_status = "error"
        state.run_error_text = error_text
        yield AgentEvent(EventType.ERROR, {"stage": stage, "error": error_text})

    # 会话历史（CORE-2 / CORE-4）：写入失败只降级为诊断；state.final_text 为空时
    # 不写 assistant，避免失败任务在共享历史里留下一条空回答污染下一轮。
    await loop._append_history(ChatMessage(role="user", content=task))
    if state.final_text.strip():
        await loop._append_history(ChatMessage(role="assistant", content=state.final_text))
    yield AgentEvent(
        EventType.DONE,
        loop._done_data(
            strategy,
            state.final_text,
            state.tier_counts,
            status=state.run_status,
            error=state.run_error_text,
        ),
    )
