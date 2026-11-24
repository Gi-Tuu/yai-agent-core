"""Agent Loop：按 Adaptive Router 选定的策略执行任务。

- direct: 一次模型调用直接回答
- react:  推理 <-> 工具调用循环，直到模型不再调用工具
- plan:   先生成步骤计划，再带计划进入 react 循环
- clarify: 通过 Channel 反问后重新路由
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import replace

from yai_core.diagnostics import (
    DiagnosticEvent,
    DiagnosticKind,
    DiagnosticLevel,
    DiagnosticSink,
    emit,
)
from yai_core.kernel.astream import (  # noqa: F401 - re-exported for compatibility
    _BUDGET_SCOPE_LABELS,
    _BUDGET_STOP_TEXT,
    _SYSTEM_TEMPLATE,
    AstStreamState,
    run_astream,
)
from yai_core.kernel.budget import Budget, BudgetExceeded, BudgetTracker
from yai_core.kernel.compaction import CompactionPolicy, compact_context
from yai_core.kernel.context import Context
from yai_core.kernel.meta_tools import MetaToolRuntime
from yai_core.kernel.planning import PlanningMixin
from yai_core.kernel.protocol import (
    _RAW_TOOL_CALL_NOTICE,
    _looks_like_raw_tool_call,
    _mark_model_stage,
    _set_outcome,
    wrap_tool_output,
)
from yai_core.kernel.react import run_react_cycle
from yai_core.kernel.router import AdaptiveRouter, RouteDecision
from yai_core.kernel.state import HostStateRegistry
from yai_core.kernel.tiering import STANDARD, STRONG, TierPolicy
from yai_core.spi import (
    Channel,
    MemoryStore,
    ModelProvider,
    ToolDiscovery,
)
from yai_core.tools.executor import ToolExecutor
from yai_core.tools.meta import REPLAN, build_replan_tool
from yai_core.tools.registry import ToolRegistry
from yai_core.types import (
    AgentEvent,
    ChatMessage,
    EventType,
    ModelResponse,
    Strategy,
    Subject,
    ToolCallRequest,
)

# 澄清反问的固定话术：不调用模型，离线可测；明确告诉用户需要补什么。
# 第 2 条把领域名词降级为举例——内核是通用的，不能写死某个宿主的业务对象。
_CLARIFY_QUESTION = (
    "我没完全理解“{task}”该怎么执行。请补充：\n"
    "1）你想做什么（查询 / 统计 / 记录 / 新建 / 完成）；\n"
    "2）涉及哪个对象或哪件事（例如某位客户、某类订单、某个待办）。"
)
# 澄清卡片里内嵌的任务最大长度，超长截断，避免长任务把宿主 UI 撑爆。
_CLARIFY_TASK_PREVIEW_CHARS = 80
# 澄清被放弃（空回答/超时/用户终止）时的收尾文案。
_CLARIFY_ABORT_TEXT = (
    "任务信息不足，已停止。请换个说法重新描述你想做什么，例如“统计华东区硬件订单的销售额”。"
)
#: 单次模型调用的默认硬超时（秒）：防止模型 / 网络永久挂起把任务拖死。
#: 传 None 可关闭（由模型客户端自身的超时负责）；对第三方自定义模型同样生效，
#: 因为超时在 loop 层用 asyncio.timeout 统一兜底，而不是依赖各模型实现。
DEFAULT_MODEL_TIMEOUT = 60.0

#: 任务起始主动召回默认注入的长期记忆条数：只放最相关的少数几条，避免挤占上下文。
DEFAULT_AUTO_RECALL_K = 3

#: 注入上下文的单条主动召回记忆正文字符上限（事件与提示都做收敛，防止刷屏）。
_AUTO_RECALL_PREVIEW_CHARS = 120


class AgentLoop(PlanningMixin, MetaToolRuntime):
    def __init__(
        self,
        model: ModelProvider,
        registry: ToolRegistry,
        executor: ToolExecutor,
        channel: Channel,
        memory: MemoryStore,
        router: AdaptiveRouter | None = None,
        discovery: ToolDiscovery | None = None,
        *,
        max_iters: int = 6,
        max_clarify_rounds: int = 2,
        full_schema_budget: int = 24,
        strict_direct: bool = False,
        meta_tools: Iterable[str] = (),
        compaction_policy: CompactionPolicy | None = None,
        compaction_model: ModelProvider | None = None,
        tier_policy: TierPolicy | None = None,
        model_timeout: float | None = DEFAULT_MODEL_TIMEOUT,
        on_diagnostic: DiagnosticSink | None = None,
        budget: Budget | None = None,
        auto_recall: Callable[[str], Awaitable[list[dict[str, object]]]] | None = None,
        auto_recall_k: int = DEFAULT_AUTO_RECALL_K,
        host_state: HostStateRegistry | None = None,
        tool_output_guard: bool = True,
        # max_clarify_rounds：澄清反问的最大轮数（正整数）；0 或负数等价于不澄清、
        # 路由判 clarify 时立即降级直接回答。
        # full_schema_budget：工具数不超过该值时，一次性把全部完整 JSON Schema
        # 注入 function-calling（零额外模型调用，当前宿主规模都走这条）；超过后
        # 改为"先看轻量目录选工具、再注入所选完整 schema"的两层懒加载，避免上百
        # 工具把上下文撑爆。聚焦失败一律回退全量，宁多勿漏。
        # meta_tools：由内核直接拦截处理的工具名（如 request_capability），
        # 它们注册进目录让模型可见，但不进 ToolExecutor 的普通 handler 执行路径。
        # compaction_policy：上下文摘要压缩策略；None（默认）= 关闭、只走硬裁剪，
        # 非 None 时在 react 每轮模型调用前检查高水位，把旧轮压成摘要并发事件。
        # compaction_model：摘要专用模型；None（默认）时压缩也走主模型，宿主可
        # 注入更便宜的小模型专门做摘要以省钱（预算计数本就不含压缩调用）。
    ) -> None:
        self.model = model
        self.registry = registry
        self.executor = executor
        self.channel = channel
        self.memory = memory
        self.router = router or AdaptiveRouter()
        # 工具发现源：默认 None（安全默认，只发缺口事件、不动态注册）；
        # 宿主显式注入（如 StaticCatalog）后，缺口出现时才会发现并注册新工具。
        self.discovery = discovery
        self.max_iters = max_iters
        self.max_clarify_rounds = max_clarify_rounds
        self.full_schema_budget = full_schema_budget
        # direct 策略下模型自带 tool_calls 的处理：默认沿用旧行为（执行），
        # 宿主显式开启后失败关闭。不能默认收紧——现有宿主依赖模型在 direct 下补调工具。
        self.strict_direct = strict_direct
        self.meta_tools: set[str] = set(meta_tools)
        # 摘要压缩：默认 None（关闭，行为与旧版一致）；宿主显式传入后启用。
        self.compaction_policy = compaction_policy
        # 摘要专用模型：None 时压缩走主模型；传入小模型则摘要改走它（省钱）。
        self.compaction_model = compaction_model
        # 成本·延迟感知选档：None（关闭）时执行轮恒走 standard（旧行为）；
        # 传入 TierPolicy 后按"该花才花"在规划/失败/后段升 strong。
        self.tier_policy = tier_policy
        # 单次模型调用硬超时（秒）；None=关闭，交给模型客户端自身超时。
        self.model_timeout = model_timeout
        # 本地诊断回调（旁路排障）；None=安静运行。同步、零依赖、异常不反噬。
        self._on_diagnostic = on_diagnostic
        # 宿主实时状态：注册表由 AgentCore 持有，这里只读渲染；None = 不注入。
        self.host_state = host_state
        # 工具输出是否包成不可信数据块（见 wrap_tool_output）。默认开。
        self.tool_output_guard = tool_output_guard
        # 当前任务原文：长任务被裁剪/压缩后，用它把目标重新锚回 prompt（见 _model_messages）。
        self._current_task = ""
        # 同一次任务里状态没变就不重复发事件（避免每轮刷一条同样的注入记录）。
        self._last_state_fingerprint: str | None = None
        # 鸭子接线：把同一回调交给路由与模型后端，只要它们暴露可写的
        # on_diagnostic 属性（AdaptiveRouter / FallbackModelProvider 都支持）。
        # 宿主自建、不认识该属性的自定义组件不受影响。
        if hasattr(self.router, "on_diagnostic"):
            self.router.on_diagnostic = on_diagnostic
        if hasattr(self.model, "on_diagnostic"):
            self.model.on_diagnostic = on_diagnostic
        if self.compaction_model is not None and hasattr(
            self.compaction_model, "on_diagnostic"
        ):
            self.compaction_model.on_diagnostic = on_diagnostic
        # 任务级预算规格（模型/工具/token 硬上限）；None=不限制。
        # 真正的计数器在每次 astream 顶层任务开始时新建（_active_budget），
        # 澄清递归共享同一计数器，任务结束即废弃。
        self.budget = budget
        self._active_budget: BudgetTracker | None = None
        # 任务起始主动召回钩子：None（默认）= 关闭，行为与不接长期记忆一致。
        # 传入一个 async (task) -> list[记忆dict] 的回调（由 Core 用 vault.arecall
        # 绑定 k 后注入），任务开始、进入规划/执行前先把相关长期记忆塞进上下文，
        # 模型无需"先想到调 recall"就能用上用户画像与历史。
        self._auto_recall = auto_recall
        self._auto_recall_k = auto_recall_k

    def _diag(
        self,
        kind: DiagnosticKind | str,
        message: str,
        *,
        level: DiagnosticLevel = DiagnosticLevel.WARNING,
        **data: object,
    ) -> None:
        """安全发出一条本地诊断（回调异常被 emit 吞掉，不影响主流程）。"""
        emit(
            self._on_diagnostic,
            DiagnosticEvent(kind=str(kind), message=message, level=level, data=data),
        )

    async def _inject_auto_recall(
        self, ctx: Context, task: str
    ) -> AsyncIterator[AgentEvent]:
        """任务开始时主动召回相关长期记忆并注入上下文；未配置或无命中则静默跳过。

        记忆是增强而非必需：召回回调任何异常都降级为"不注入"，只发一条本地诊断，
        绝不让记忆子系统故障阻断任务。命中时发 MEMORIES_RECALLED 事件（可观测、
        不静默决策），并把条目作为一条 system 消息放到历史之后、user 任务之前。
        """
        if self._auto_recall is None:
            return
        try:
            if self.model_timeout is None:
                hits = await self._auto_recall(task)
            else:
                # 召回可能走宿主 embedder（网络 / 重计算）：用模型硬超时同一额度
                # 兜底。否则一次卡死的召回会把任务永远挂在 STRATEGY_SELECTED 之后，
                # 而 model_timeout 根本覆盖不到这段（审计 F9）。
                async with asyncio.timeout(self.model_timeout):
                    hits = await self._auto_recall(task)
        except TimeoutError:
            self._diag(
                "auto_recall_timeout",
                f"任务起始主动召回超过 {self.model_timeout:g}s，已跳过注入",
                level=DiagnosticLevel.WARNING,
                timeout=self.model_timeout,
            )
            return
        except Exception as exc:  # noqa: BLE001 - 记忆增强失败不阻断主任务
            self._diag("auto_recall_failed", f"任务起始主动召回失败: {exc}")
            return
        if not isinstance(hits, list) or not hits:
            return
        lines: list[str] = []
        event_items: list[dict[str, object]] = []
        for hit in hits[: self._auto_recall_k]:
            if not isinstance(hit, dict):
                continue
            content = str(hit.get("content", "")).strip()
            if not content:
                continue
            kind = str(hit.get("kind", "note"))
            lines.append(f"- （{kind}）{content}")
            event_items.append(
                {
                    "id": hit.get("id", ""),
                    "kind": kind,
                    "content": content[:_AUTO_RECALL_PREVIEW_CHARS],
                }
            )
        if not lines:
            return
        ctx.add(
            ChatMessage(
                role="system",
                content=(
                    "以下是与当前任务相关的长期记忆，供你参考；若与用户本次的"
                    "明确指令冲突，以用户本次指令为准：\n" + "\n".join(lines)
                ),
            )
        )
        yield AgentEvent(
            EventType.MEMORIES_RECALLED,
            {"count": len(event_items), "items": event_items},
        )

    async def _model_messages(
        self, ctx: Context
    ) -> tuple[list[dict], AgentEvent | None]:
        """产出给模型的消息，并在每次调用前重新取一次宿主实时状态。

        后端不支持多模态时自动降级成纯文本（口径在 :meth:`Context.llm_messages`）：
        图片与资源引用换成"模型看不到它"的占位文本——发一个后端不认的数组，
        比明说缺资料更糟。

        宿主状态是**瞬态**的：只塞进本次消息，不写回 ``ctx``；状态内容没变时
        不再重复发 ``HOST_STATE`` 事件，变化或首次注入时才发。
        """
        # 硬上限是最终安全网：启用压缩时 add 不再即时裁剪，这里保证任何模型调用
        # 都不超过 Context.max_chars（按轮首对齐，不会拆散工具配对）。
        ctx.enforce_budget()
        transient = ""
        event: AgentEvent | None = None
        if self.host_state is not None:
            try:
                body, info = await self.host_state.render(
                    on_diagnostic=self._on_diagnostic
                )
            except Exception as exc:  # noqa: BLE001 - 状态是增强，不能拖垮模型调用
                self._diag(
                    DiagnosticKind.HOST_STATE_FAILED,
                    f"宿主状态渲染失败（{type(exc).__name__}），本轮未注入",
                    level=DiagnosticLevel.WARNING,
                    error=f"{type(exc).__name__}: {exc}",
                )
                body, info = "", None
            if body and info is not None:
                transient = body
                fingerprint = str(info.get("fingerprint", ""))
                if fingerprint != self._last_state_fingerprint:
                    self._last_state_fingerprint = fingerprint
                    event = AgentEvent(EventType.HOST_STATE, info)
        # 上下文腐化防护：长任务被裁剪 / 压缩后，原始任务可能已经不在消息里。
        # 与其让模型靠猜，不如把目标重新锚到 system 位（模型对头部注意力最强）。
        task = self._current_task
        if task and not any(
            message.role == "user" and task[:40] in (message.content or "")
            for message in ctx.messages[1:]
        ):
            anchor = f"【当前任务】{task[:500]}"
            transient = transient + "\n\n" + anchor if transient else anchor
        messages = ctx.llm_messages(
            multimodal=bool(getattr(self.model, "yai_multimodal", False)),
            transient_system=transient,
        )
        return messages, event

    async def _compact_before_model(self, ctx: Context) -> AsyncIterator[AgentEvent]:
        """模型调用前的上下文准备：先摘要压缩，再硬裁剪兜底。

        压缩启用时 ``Context.add`` 不再即时裁剪（``trim_on_add=False``），否则旧轮
        会在压缩器看到它之前就被删掉，摘要永远等不到高水位。这里按轮边界先压、再裁，
        保证发给模型的消息既保留要点又不超硬上限。
        """
        policy = self.compaction_policy
        if policy is not None:
            if policy.hard_max_chars > ctx.max_chars:
                # 水位高于 Context 硬上限时永远等不到触发，按硬上限收紧。
                policy = replace(policy, hard_max_chars=ctx.max_chars)
            compact_ev = await compact_context(
                ctx,
                self.compaction_model or self.model,
                policy,
                timeout=self.model_timeout,
                on_diagnostic=self._on_diagnostic,
            )
            if compact_ev is not None:
                yield compact_ev
        ctx.enforce_budget()

    async def _model_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        *,
        tier: str,
        tier_counts: dict[str, int] | None = None,
    ) -> ModelResponse:
        """统一模型调用入口：套一层硬超时，超时转带说明的 TimeoutError。

        - 超时：``asyncio.timeout`` 会先取消挂起的模型调用协程，再抛 TimeoutError，
          因此挂死的调用不会残留；上层据此发 ERROR 事件体面收尾。
        - 外层取消（用户终止 / aclose / 任务 cancel）：抛 CancelledError，
          它属于 BaseException、不会被这里或上层的 ``except Exception`` 吞掉，
          因而能干净传播、真正中断任务。

        ``tier_counts`` 给了就按档位记录**已发起**的调用次数（审计 F12）：计数
        放在预算闸之后、真正调用之前，因此被预算拒绝的尝试不会计入 DONE，与
        :class:`~yai_core.kernel.budget.BudgetTracker` 的"尝试"口径严格一致。
        """
        tracker = self._active_budget
        if tracker is not None:
            # 调用前先过预算硬墙：已达模型调用上限则本次不再发起（熔断）。
            tracker.before_model_call()
        if tier_counts is not None:
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        if self.model_timeout is None:
            try:
                resp = await self.model.achat(messages, tools=tools, tier=tier)
            except Exception as exc:
                _mark_model_stage(exc)
                raise
        else:
            try:
                # 用 asyncio.timeout 而不是 asyncio.wait_for：3.11 的 wait_for 会把
                # 协程包进额外 Task，并在"内层恰好完成"时走 `if fut.done(): return
                # fut.result()` 把外层取消吞掉——委派里"一个子任务失败要中断另一个
                # 子任务"就靠取消生效，吞掉就等于没人中断，工具会一直跑下去。
                # asyncio.timeout 是 3.12+ wait_for 的同一套语义，各版本行为一致。
                async with asyncio.timeout(self.model_timeout):
                    resp = await self.model.achat(messages, tools=tools, tier=tier)
            except TimeoutError:
                timeout_error = TimeoutError(
                    f"模型调用超过 {self.model_timeout:g}s 硬超时上限，已终止"
                )
                _mark_model_stage(timeout_error)
                raise timeout_error from None
            except Exception as exc:
                _mark_model_stage(exc)
                raise
        if tracker is not None:
            # 成功返回后计数并累计 token；若因此越过 token 上限会在此熔断。
            tracker.record_model_call(resp)
        return resp

    async def astream(
        self,
        task: str,
        *,
        _clarify_depth: int = 0,
        subject: Subject | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        async for event in run_astream(
            self,
            task,
            _clarify_depth=_clarify_depth,
            subject=subject,
        ):
            yield event

    def _history_messages(self) -> list[ChatMessage]:
        """读取会话历史；宿主 MemoryStore 故障降级为"本次不注入历史"（CORE-2）。"""
        try:
            return list(self.memory.history())
        except Exception as exc:  # noqa: BLE001 - 历史是增强，故障不得阻断任务
            self._diag(
                "memory_history_failed",
                f"读取会话历史失败（{type(exc).__name__}），本次不注入历史",
                level=DiagnosticLevel.ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )
            return []

    async def _append_history(self, message: ChatMessage) -> None:
        """写入会话历史；宿主 MemoryStore 故障只记诊断，绝不打断收尾（CORE-2）。"""
        try:
            await self.memory.append_history(message)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不得让任务"没有终态"
            self._diag(
                "memory_append_failed",
                f"写入会话历史失败（{type(exc).__name__}），本次未持久化该条消息",
                level=DiagnosticLevel.ERROR,
                role=message.role,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _budget_snapshot(self) -> dict[str, object]:
        """当前任务预算用量快照；未启用预算时返回空字典（调用方先判空）。"""
        if self._active_budget is None:
            return {}
        return self._active_budget.snapshot()

    def _done_data(
        self,
        strategy: Strategy,
        final_text: str,
        tier_counts: dict[str, int],
        *,
        status: str = "ok",
        error: str | None = None,
    ) -> dict[str, object]:
        """组装 DONE 载荷：所有收尾路径的 schema 键必须一致（审计 F12）。

        固定键：``strategy`` / ``final_text`` / ``standard_calls`` /
        ``strong_calls`` / ``model_calls`` / ``status`` / ``error``（成功时
        ``error=None``）；配置预算时额外带 ``budget`` 快照（熔断再加 ``reason``）。
        成本计数按"尝试"口径，与 :class:`~yai_core.kernel.budget.BudgetTracker`
        一致：失败/超时的模型尝试也计入。
        """
        data: dict[str, object] = {
            "strategy": strategy.value,
            "final_text": final_text,
            "standard_calls": tier_counts[STANDARD],
            "strong_calls": tier_counts[STRONG],
            "model_calls": tier_counts[STANDARD] + tier_counts[STRONG],
            "status": status,
            "error": error,
        }
        # 配置了预算就把用量快照随 DONE 流出（成本可观测）；触发熔断时标明原因。
        if self._active_budget is not None:
            data["budget"] = self._budget_snapshot()
            if self._active_budget.exceeded is not None:
                data["reason"] = "budget_exceeded"
        return data

    async def _emit_capability_gap(
        self, decision: RouteDecision, task: str
    ) -> AsyncIterator[AgentEvent]:
        """LLM 判定现有工具不足时发缺口事件；配置发现源则闭环成新工具。

        规则路径、澄清、无工具部署形态不发；发现注册发生在系统提示构建之前，
        react/plan 本轮即可看到新工具。
        """
        if (
            decision.missing_capability
            and decision.strategy != Strategy.CLARIFY
            and len(self.registry) > 0
        ):
            yield AgentEvent(
                EventType.CAPABILITY_MISSING,
                {
                    "task": task,
                    "missing": decision.missing_capability,
                    "available_tools": [s.name for s in self.registry.all()],
                    "strategy": decision.strategy.value,
                },
            )
            if self.discovery is not None:
                async for ev in self._discover_tools(decision.missing_capability, task):
                    yield ev

    async def _run_clarify(
        self, task: str, depth: int, subject: Subject | None = None
    ) -> AsyncIterator[AgentEvent]:
        """向用户反问并累积补充后重新路由；空回答体面收尾，绝不无限反问。"""
        preview = task.strip()
        if len(preview) > _CLARIFY_TASK_PREVIEW_CHARS:
            preview = preview[:_CLARIFY_TASK_PREVIEW_CHARS] + "…"
        question = _CLARIFY_QUESTION.format(task=preview)
        yield AgentEvent(
            EventType.CLARIFY_REQUESTED,
            {"question": question, "round": depth + 1},
        )
        answer = ""
        try:
            answer = (await self.channel.ask(question) or "").strip()
        except Exception as exc:  # noqa: BLE001 - 通道故障不得让任务没有终态
            self._diag(
                "clarify_channel_failed",
                f"澄清通道调用失败（{type(exc).__name__}），按“未获得补充信息”收尾",
                level=DiagnosticLevel.ERROR,
                round=depth + 1,
                error=f"{type(exc).__name__}: {exc}",
            )
            answer = ""
        if not answer:
            # 空回答 / 超时 / 通道故障 / 用户终止：体面收尾，不再递归。
            yield AgentEvent(EventType.MODEL_MESSAGE, {"text": _CLARIFY_ABORT_TEXT})
            await self._append_history(ChatMessage(role="user", content=task))
            await self._append_history(
                ChatMessage(role="assistant", content=_CLARIFY_ABORT_TEXT)
            )
            yield AgentEvent(
                EventType.DONE,
                self._done_data(
                    Strategy.CLARIFY,
                    _CLARIFY_ABORT_TEXT,
                    {STANDARD: 0, STRONG: 0},
                    status="error",
                    error="clarify_aborted",
                ),
            )
            return
        # 关键：把原任务与历次补充累积起来再重新路由，避免"问意图→问公司"的乒乓。
        merged = f"{task}\n补充信息：{answer}"
        async for ev in self.astream(
            merged, _clarify_depth=depth + 1, subject=subject
        ):
            yield ev

    async def _react_cycle(
        self,
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
        async for event in run_react_cycle(
            self,
            ctx,
            use_tools=use_tools,
            task=task,
            strategy=strategy,
            tier_policy=tier_policy,
            tier_counts=tier_counts,
            subject=subject,
            outcome=outcome,
        ):
            yield event

    def _normalize_tool_call_ids(
        self, calls: list[ToolCallRequest], iteration: int
    ) -> list[ToolCallRequest] | None:
        """保证本轮 tool_call id 非空且唯一。

        空 id 合成稳定 id；重复 id 返回 None，由调用方失败关闭、不执行任何工具。
        重复 id 若照常执行，会让有副作用的 handler 跑两次，并在下一轮生成
        OpenAI 兼容接口拒绝的重复 tool_call_id。
        """
        seen: set[str] = set()
        normalized: list[ToolCallRequest] = []
        synthesized = False
        for index, call in enumerate(calls):
            call_id = (call.id or "").strip()
            if not call_id:
                call_id = f"call_{iteration}_{index}"
                synthesized = True
            if call_id in seen:
                return None
            seen.add(call_id)
            normalized.append(
                call if call_id == call.id else replace(call, id=call_id)
            )
        if synthesized:
            self._diag(
                DiagnosticKind.MODEL_ERROR,
                "模型返回了空 tool_call id，已合成唯一 id 后继续",
                stage="react",
                iteration=iteration,
            )
        return normalized

    def _append_assistant_tool_calls(self, ctx: Context, resp: ModelResponse) -> None:
        """把模型本轮的 tool_calls 作为 assistant 消息加入上下文（含 JSON 参数）。"""
        ctx.add(
            ChatMessage(
                role="assistant",
                content=resp.content,
                tool_calls=[
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in resp.tool_calls
                ],
                raw_content_blocks=resp.raw_content_blocks,
            )
        )

    async def _execute_tool_calls(
        self,
        calls: list[ToolCallRequest],
        ctx: Context,
        task: str,
        failed: list[str],
        newly_registered: list[str],
        replanned: list[int],
        tier_policy: TierPolicy | None,
        tier_counts: dict[str, int] | None,
        subject: Subject | None = None,
        terminal_messages: list[str] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """顺序执行本轮工具：meta-tool 由内核直接处理，普通工具走执行器。

        失败描述与新注册工具名分别写入传入的两个列表，供调用方更新 schema、
        回灌反思；工具结果消息统一加入上下文。
        """
        for call in calls:
            if call.name in self.meta_tools:
                # 内核 meta-tool（请求发现/创建工具/重规划）由 loop 直接处理，
                # 可能在执行中动态注册新工具，不进普通执行器。
                async for ev in self._handle_meta_tool(
                    call,
                    ctx,
                    task,
                    replanned,
                    tier_policy,
                    tier_counts,
                    subject,
                ):
                    yield ev
                    if ev.type == EventType.TOOL_DISCOVERED:
                        newly_registered.extend(ev.data.get("registered", []))
                    elif ev.type == EventType.CODE_TOOL_CREATED:
                        newly_registered.append(ev.data.get("name", ""))
                continue
            # 预算计数由执行器负责：组合工具的真实步骤也会逐次经过同一道闸，
            # 不能只把"一次组合"记成一次业务调用。超限异常冒泡到 astream 统一熔断。
            tool_events, ok, result_text = await self.executor.execute(
                call.name,
                call.arguments,
                subject=subject,
                budget=self._active_budget,
                call_id=call.id,
            )
            for tool_event in tool_events:
                yield tool_event
            ctx.add(
                ChatMessage(
                    role="tool",
                    content=(
                        wrap_tool_output(result_text)
                        if self.tool_output_guard
                        else result_text
                    ),
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
            stale = next(
                (
                    event
                    for event in tool_events
                    if event.type == EventType.TOOL_RESULT
                    and event.data.get("preview_stale")
                ),
                None,
            )
            if stale is not None:
                # 计划在用户确认后已变化：本次任务到此为止，不再让模型补救。
                if terminal_messages is not None:
                    terminal_messages.append(
                        str(stale.data.get("error") or "审批预览已失效，本次未执行")
                    )
                return
            if not ok:
                failed.append(f"{call.name}：{result_text}")

    def _schema_after_registration(
        self, newly_registered: list[str], focused_names: list[str] | None
    ) -> list[dict]:
        """新工具注册后重算下一轮 function-calling 的完整 schema 集合。"""
        if focused_names is None:
            return self.registry.llm_schemas()
        for name in newly_registered:
            if name not in focused_names:
                focused_names.append(name)
        return self._with_meta_schemas(self.registry.schemas_for(focused_names))

    def _with_meta_schemas(self, schemas: list[dict]) -> list[dict]:
        """把内核 meta-tool 的完整 schema 合并进注入集（聚焦裁剪不丢 meta）。"""
        if not self.meta_tools:
            return schemas
        present = {s.get("function", {}).get("name") for s in schemas}
        merged = list(schemas)
        for spec in self.registry.all():
            if spec.name in self.meta_tools and spec.name not in present:
                merged.append(spec.llm_schema())
        return merged

    def _ensure_replan_tool(self) -> None:
        """PLAN 策略下幂等注册 replan meta-tool（仅多步计划执行需要）。"""
        if REPLAN not in self.meta_tools:
            self.registry.register(build_replan_tool())
            self.meta_tools.add(REPLAN)

    def _append_failure_reflection(self, ctx: Context, failed: list[str]) -> None:
        """把失败观察回灌成明确引导，让模型下一轮自我修正而非重复失败调用。"""
        ctx.add(
            ChatMessage(
                role="user",
                content=(
                    "【系统反思提示】上一步工具调用未成功："
                    + "；".join(failed)
                    + "。请判断：若能用其他现有工具完成，就改用其他工具，"
                    "不要重复同一失败调用；若宿主确实缺少所需能力，"
                    "可调用 request_capability 请求发现新工具，"
                    "否则请直接说明缺失了什么能力，并给出当前能给出的结论。"
                ),
            )
        )

    async def _finish_at_iteration_limit(
        self,
        ctx: Context,
        tier_counts: dict[str, int] | None = None,
        outcome: dict[str, object] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """达到工具调用上限：要求模型基于已有观察直接收尾。"""
        ctx.add(
            ChatMessage(
                role="user",
                content="已达到工具调用上限，请基于已有结果直接给出最终答案。",
            )
        )
        limit_data = {
            "tier": STANDARD,
            "iteration_limit": True,
            "max_iters": self.max_iters,
            "reason": "iteration_limit",
        }
        try:
            messages, state_event = await self._model_messages(ctx)
            if state_event is not None:
                yield state_event
            resp = await self._model_chat(
                messages, None, tier=STANDARD, tier_counts=tier_counts
            )
        except BudgetExceeded:
            # 预算熔断仍由 astream 统一收尾；取消也不转成普通失败。
            raise
        except Exception as exc:  # noqa: BLE001 - 保留上限决策，错误仍交给 astream 统一处理
            # 没有模型答案就保持原有空收尾，不把合成文案写入历史；
            # 结局由 astream 的兜底分支置 error（异常已带 model 标记）。
            _set_outcome(outcome, "error", f"{type(exc).__name__}: {exc}")
            yield AgentEvent(EventType.MODEL_MESSAGE, {"text": "", **limit_data})
            raise
        final_text = resp.content.strip()
        if _looks_like_raw_tool_call(final_text):
            # 限尾收尾与正常轮走同一道协议检查：模型把 <tool_call> 写进正文时，
            # 既不能当最终答案展示（用户会看到一段假调用），也不能写进历史——
            # 否则伪协议文本会绕过 _looks_like_raw_tool_call 的唯一防线。
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
                {"text": _RAW_TOOL_CALL_NOTICE, **limit_data},
            )
            _set_outcome(outcome, "error", _RAW_TOOL_CALL_NOTICE)
            return
        if not final_text and resp.tool_calls:
            # 限尾收尾仍要求调工具（结构化 tool_calls）：既不能当最终答案，也不能
            # 静默产出空回答（审计 F6）——明确报错 + 非空诚实文案，宿主不再白屏。
            honest = (
                "已达到工具调用上限，且模型未能基于已有结果收尾。请重试，"
                "或把任务拆成更小的步骤分别执行。"
            )
            error = (
                f"iteration_limit: 限尾收尾仍返回 {len(resp.tool_calls)} 个工具调用，"
                "没有可交付的最终答案"
            )
            _set_outcome(outcome, "error", error)
            yield AgentEvent(
                EventType.ERROR, {"stage": "iteration_limit", "error": error}
            )
            yield AgentEvent(EventType.MODEL_MESSAGE, {"text": honest, **limit_data})
            return
        yield AgentEvent(
            EventType.MODEL_MESSAGE,
            {"text": final_text, **limit_data},
        )
