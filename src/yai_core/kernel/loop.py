"""Agent Loop：按 Adaptive Router 选定的策略执行任务。

- direct: 一次模型调用直接回答
- react:  推理 <-> 工具调用循环，直到模型不再调用工具
- plan:   先生成步骤计划，再带计划进入 react 循环
- clarify: 通过 Channel 反问后重新路由
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

from yai_core.diagnostics import (
    DiagnosticEvent,
    DiagnosticKind,
    DiagnosticLevel,
    DiagnosticSink,
    emit,
)
from yai_core.kernel.budget import Budget, BudgetExceeded, BudgetTracker
from yai_core.kernel.compaction import CompactionPolicy, compact_context
from yai_core.kernel.context import Context
from yai_core.kernel.router import AdaptiveRouter, RouteDecision
from yai_core.kernel.tiering import STANDARD, STRONG, TierPolicy
from yai_core.policy.authorize import authorize_tool_call, snapshot_arguments
from yai_core.spi import (
    Channel,
    MemoryStore,
    ModelProvider,
    ToolDiscovery,
)
from yai_core.tools.executor import ToolExecutor
from yai_core.tools.meta import (
    CREATE_CODE_TOOL,
    REPLAN,
    REQUEST_CAPABILITY,
    build_replan_tool,
)
from yai_core.tools.registry import ToolRegistry
from yai_core.types import (
    AgentEvent,
    ChatMessage,
    EventType,
    ModelResponse,
    Strategy,
    Subject,
    ToolCallRequest,
    ToolSpec,
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


#: 单次模型调用的默认硬超时（秒）：防止模型 / 网络永久挂起把任务拖死。
#: 传 None 可关闭（由模型客户端自身的超时负责）；对第三方自定义模型同样生效，
#: 因为超时在 loop 层用 asyncio.wait_for 统一兜底，而不是依赖各模型实现。
DEFAULT_MODEL_TIMEOUT = 60.0

#: 任务起始主动召回默认注入的长期记忆条数：只放最相关的少数几条，避免挤占上下文。
DEFAULT_AUTO_RECALL_K = 3

#: 注入上下文的单条主动召回记忆正文字符上限（事件与提示都做收敛，防止刷屏）。
_AUTO_RECALL_PREVIEW_CHARS = 120


class AgentLoop:
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
        meta_tools: Iterable[str] = (),
        compaction_policy: CompactionPolicy | None = None,
        compaction_model: ModelProvider | None = None,
        tier_policy: TierPolicy | None = None,
        model_timeout: float | None = DEFAULT_MODEL_TIMEOUT,
        on_diagnostic: DiagnosticSink | None = None,
        budget: Budget | None = None,
        auto_recall: Callable[[str], Awaitable[list[dict[str, object]]]] | None = None,
        auto_recall_k: int = DEFAULT_AUTO_RECALL_K,
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
            hits = await self._auto_recall(task)
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

    def _model_messages(self, ctx: Context) -> list[dict]:
        """产出给模型的消息：后端不支持多模态时自动降级成纯文本。

        降级口径在 :meth:`Context.llm_messages` 里：图片与资源引用换成"模型看不到
        它"的占位文本。后端没声明 ``yai_multimodal`` 时按"不支持"处理——发一个
        后端不认的数组，比明说缺资料更糟。
        """
        return ctx.llm_messages(
            multimodal=bool(getattr(self.model, "yai_multimodal", False))
        )

    async def _model_chat(self, messages, tools, *, tier: str) -> ModelResponse:
        """统一模型调用入口：套一层硬超时，超时转带说明的 TimeoutError。

        - 超时：``wait_for`` 会先取消挂起的模型调用协程，再抛 TimeoutError，
          因此挂死的调用不会残留；上层据此发 ERROR 事件体面收尾。
        - 外层取消（用户终止 / aclose / 任务 cancel）：抛 CancelledError，
          它属于 BaseException、不会被这里或上层的 ``except Exception`` 吞掉，
          因而能干净传播、真正中断任务。
        """
        tracker = self._active_budget
        if tracker is not None:
            # 调用前先过预算硬墙：已达模型调用上限则本次不再发起（熔断）。
            tracker.before_model_call()
        if self.model_timeout is None:
            resp = await self.model.achat(messages, tools=tools, tier=tier)
        else:
            try:
                resp = await asyncio.wait_for(
                    self.model.achat(messages, tools=tools, tier=tier),
                    timeout=self.model_timeout,
                )
            except TimeoutError:
                raise TimeoutError(
                    f"模型调用超过 {self.model_timeout:g}s 硬超时上限，已终止"
                ) from None
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
    ) -> AsyncIterator[AgentEvent]:
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
        if _clarify_depth == 0:
            self._active_budget = self.budget.start() if self.budget is not None else None
        decision = await self.router.aclassify(
            task, self.registry, discoverable=self._discoverable_catalog()
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
        async for ev in self._emit_capability_gap(decision, task):
            yield ev

        if strategy == Strategy.CLARIFY and _clarify_depth >= self.max_clarify_rounds:
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
            async for ev in self._run_clarify(task, _clarify_depth, subject):
                yield ev
            return

        # 两层工具目录：系统提示只放 name + 一句话摘要（轻量目录），
        # 完整参数结构在 function-calling 的 tools 参数里按需注入，见 _react_cycle。
        ctx = Context(_SYSTEM_TEMPLATE.format(tools=self.registry.catalog_text()))
        for msg in self.memory.history():
            ctx.add(msg)
        # 任务起始主动召回：把与本任务相关的长期记忆作为一条 system 上下文注入，
        # 位于历史之后、user 任务之前。clarify 路径已在上方提前 return，不做召回。
        async for recall_event in self._inject_auto_recall(ctx, task):
            yield recall_event
        ctx.add(ChatMessage(role="user", content=task))

        # 模型档位调用计数（成本证据）：贯穿规划 + 执行（含工具聚焦、迭代收尾、
        # 重规划）；不含路由分类与上下文压缩调用，口径在讲义说明。
        tier_counts: dict[str, int] = {STANDARD: 0, STRONG: 0}

        final_text = ""
        try:
            if strategy == Strategy.PLAN:
                # replan meta-tool 只在多步计划执行中需要，进入 PLAN 时幂等注册，
                # 让模型在计划走不通时显式触发重规划（空宿主 / direct / 简单 react 不注册）。
                self._ensure_replan_tool()
                async for ev in self._make_plan(
                    ctx,
                    task,
                    tier=decision.tier,
                    tier_policy=self.tier_policy,
                    tier_counts=tier_counts,
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

            async for ev in self._react_cycle(
                ctx,
                use_tools=strategy != Strategy.DIRECT,
                task=task,
                strategy=strategy,
                tier_policy=self.tier_policy,
                tier_counts=tier_counts,
                subject=subject,
            ):
                yield ev
                if ev.type == EventType.MODEL_MESSAGE:
                    final_text = ev.data.get("text", final_text)
        except BudgetExceeded as exc:
            # 预算硬墙：规划/聚焦/执行/收尾任一处触发都在此统一熔断，
            # 不再发起新的模型或工具调用，用固定诚实文案收尾并发可审计事件。
            label = _BUDGET_SCOPE_LABELS.get(exc.scope, exc.scope)
            stop_text = _BUDGET_STOP_TEXT.format(label=label)
            final_text = stop_text
            yield AgentEvent(EventType.MODEL_MESSAGE, {"text": stop_text})
            yield AgentEvent(
                EventType.BUDGET_EXCEEDED,
                {
                    "scope": exc.scope,
                    "limit": exc.limit,
                    "used": exc.used,
                    "budget": self._budget_snapshot(),
                },
            )
        except Exception as exc:  # noqa: BLE001 - 模型超时/失败体面收尾，不穿透到宿主
            # 规划 / 收尾 / 重规划等未被局部捕获的模型异常（含硬超时）在此兜底：
            # 发 ERROR 事件后仍照常落 DONE，宿主拿到有界结果而不是裸异常。
            # react 主轮的模型异常已在循环内局部转 ERROR，不会走到这里。
            # CancelledError 属于 BaseException，不被本分支捕获，从而能干净传播、
            # 真正响应用户终止 / aclose / 任务取消（见 _model_chat）。
            is_timeout = isinstance(exc, TimeoutError)
            self._diag(
                DiagnosticKind.MODEL_TIMEOUT if is_timeout else DiagnosticKind.MODEL_ERROR,
                (
                    f"模型调用超过 {self.model_timeout:g}s 硬超时，已体面收尾"
                    if is_timeout
                    else f"模型调用失败（{type(exc).__name__}），已体面收尾"
                ),
                level=DiagnosticLevel.ERROR,
                stage="model",
                error=f"{type(exc).__name__}: {exc}",
            )
            yield AgentEvent(
                EventType.ERROR,
                {"stage": "model", "error": f"{type(exc).__name__}: {exc}"},
            )

        await self.memory.append_history(ChatMessage(role="user", content=task))
        await self.memory.append_history(ChatMessage(role="assistant", content=final_text))
        done_data: dict[str, object] = {
            "strategy": strategy.value,
            "final_text": final_text,
            "standard_calls": tier_counts[STANDARD],
            "strong_calls": tier_counts[STRONG],
            "model_calls": tier_counts[STANDARD] + tier_counts[STRONG],
        }
        # 配置了预算就把用量快照随 DONE 流出（成本可观测）；触发熔断时标明原因。
        if self._active_budget is not None:
            done_data["budget"] = self._budget_snapshot()
            if self._active_budget.exceeded is not None:
                done_data["reason"] = "budget_exceeded"
        yield AgentEvent(EventType.DONE, done_data)

    def _budget_snapshot(self) -> dict[str, object]:
        """当前任务预算用量快照；未启用预算时返回空字典（调用方先判空）。"""
        if self._active_budget is None:
            return {}
        return self._active_budget.snapshot()

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
        answer = (await self.channel.ask(question) or "").strip()
        if not answer:
            # 空回答 / 超时 / 用户终止：体面收尾，不再递归。
            yield AgentEvent(EventType.MODEL_MESSAGE, {"text": _CLARIFY_ABORT_TEXT})
            await self.memory.append_history(ChatMessage(role="user", content=task))
            await self.memory.append_history(
                ChatMessage(role="assistant", content=_CLARIFY_ABORT_TEXT)
            )
            yield AgentEvent(
                EventType.DONE,
                {"strategy": Strategy.CLARIFY.value, "final_text": _CLARIFY_ABORT_TEXT},
            )
            return
        # 关键：把原任务与历次补充累积起来再重新路由，避免"问意图→问公司"的乒乓。
        merged = f"{task}\n补充信息：{answer}"
        async for ev in self.astream(
            merged, _clarify_depth=depth + 1, subject=subject
        ):
            yield ev

    def _discoverable_catalog(self) -> list[dict[str, str]] | None:
        """从发现源取"可按需启用"能力的轻量目录，过滤已注册名后交给路由。

        发现源未实现可选方法 ``list_discoverable``、或调用失败时返回 None，
        退化为"只在缺口出现后被动发现"的原有行为；该增强不构成依赖。
        只透传 name + 摘要（声明），handler 与授权都不在此暴露。
        """
        if self.discovery is None:
            return None
        getter = getattr(self.discovery, "list_discoverable", None)
        if not callable(getter):
            return None
        try:
            items = getter()
        except Exception as exc:  # noqa: BLE001 - 可发现目录是增强，失败静默退化
            self._diag(
                DiagnosticKind.DISCOVERABLE_LIST_FAILED,
                f"拉取可发现能力目录失败（{type(exc).__name__}），退化为被动发现",
                source=type(self.discovery).__name__,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        if not isinstance(items, list):
            # 返回 None / 非列表（表示无可发现项）同样退化为被动发现，不能崩溃。
            return None
        registered = {s.name for s in self.registry.all()}
        catalog = [
            {"name": str(it.get("name")), "summary": str(it.get("summary", ""))}
            for it in items
            if isinstance(it, dict) and it.get("name") and it.get("name") not in registered
        ]
        return catalog or None

    async def _discover_tools(self, need: str, task: str) -> AsyncIterator[AgentEvent]:
        """能力缺口出现时，向发现源要候选并注册：全程可观测、失败不致命。

        发现源只负责"返回候选规格"，注册去重由内核统一完成；
        工具真正执行时仍走 PermissionPolicy（发现不等于授权）。
        """
        if self.discovery is None:
            return
        try:
            candidates = await self.discovery.discover(
                need,
                task=task,
                available=[s.name for s in self.registry.all()],
            )
        except Exception as exc:  # noqa: BLE001 - 发现是增强不是依赖，失败不致命
            self._diag(
                DiagnosticKind.DISCOVERY_FAILED,
                f"工具发现源执行失败（{type(exc).__name__}），本次不注册新工具",
                level=DiagnosticLevel.ERROR,
                source=type(self.discovery).__name__,
                need=need,
                error=f"{type(exc).__name__}: {exc}",
            )
            yield AgentEvent(
                EventType.ERROR,
                {"stage": "tool_discovery", "error": f"{type(exc).__name__}: {exc}"},
            )
            return

        # 防御性去重：既跳过已注册，也跳过候选之间的同名工具。
        new_specs: list[ToolSpec] = []
        seen: set[str] = set()
        for spec in candidates:
            if spec.name in seen or self.registry.has(spec.name):
                continue
            seen.add(spec.name)
            new_specs.append(spec)
        if not new_specs:
            return

        self.registry.register_many(new_specs)
        yield AgentEvent(
            EventType.TOOL_DISCOVERED,
            {
                "missing": need,
                "source": type(self.discovery).__name__,
                "registered": [s.name for s in new_specs],
            },
        )

    async def _make_plan(
        self,
        ctx: Context,
        task: str,
        *,
        tier: str = STRONG,
        tier_policy: TierPolicy | None = None,
        tier_counts: dict[str, int] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """PLAN 策略的规划步：让模型把任务拆成 2-5 个编号步骤并写入上下文。

        正则提取步骤；一个都提取不到时退化为单步（任务本身），保证下游总有可
        执行计划。规划调用按成本感知策略给档（默认 strong、关闭时沿用路由档），
        并计入 ``tier_counts``，随后发 ``plan_created`` 事件。
        """
        prompt = (
            "把下面的任务拆成 2-5 个可执行步骤，每行一个步骤，用 1. 2. 3. 编号，"
            "只输出步骤本身：\n" + task
        )
        ctx.add(ChatMessage(role="user", content=prompt))
        # 档位：成本感知开启时由策略决定（规划默认 strong），否则沿用路由建议 tier。
        plan_tier = tier_policy.for_planning().tier if tier_policy is not None else tier
        if tier_counts is not None:
            tier_counts[plan_tier] = tier_counts.get(plan_tier, 0) + 1
        resp = await self._model_chat(self._model_messages(ctx), None, tier=plan_tier)
        steps = [s.strip() for s in re.findall(r"\d+[.、)]\s*(.+)", resp.content)]
        if not steps:
            steps = [task]
        plan_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
        ctx.add(ChatMessage(role="assistant", content=plan_text))
        yield AgentEvent(EventType.PLAN_CREATED, {"steps": steps})

    async def _focus_tool_schemas(
        self, task: str, tier_counts: dict[str, int] | None = None
    ) -> tuple[list[dict], list[str] | None]:
        """两层工具目录的第二层：决定本轮注入哪些工具的完整 JSON Schema。

        返回 ``(schemas, focused_names)``；``focused_names`` 为 None 表示全量
        （预算内或聚焦失败回退），否则是聚焦选中的工具名，供执行中发现新工具后
        把新 schema 追加进同一集合。

        - 工具数在预算内：直接返回全部完整 schema（零额外模型调用，当前宿主都走这条）；
        - 工具数超预算：先让模型只看轻量目录、选出本任务要用的工具名，再用
          ``schemas_for`` 只注入这些工具的完整 schema，给大规模工具集上下文瘦身。
        - 选择失败 / 一个都没选中：回退全量 schema，宁多勿漏（瘦身为增强而非依赖）。
        """
        if len(self.registry) <= self.full_schema_budget:
            return self.registry.llm_schemas(), None
        catalog = self.registry.catalog_text()
        prompt = (
            "下面是宿主可用工具的能力目录（名称: 一句话摘要）：\n"
            f"{catalog}\n\n"
            "为完成任务，请只输出你确定会调用的工具名称，输出 JSON 字符串数组，"
            '例如 ["search_notes", "save_memory"]，不要输出任何其他内容；'
            "拿不准的工具也请一并选上，避免漏掉必要能力。\n"
            f"任务：{task}"
        )
        try:
            if tier_counts is not None:
                tier_counts[STANDARD] = tier_counts.get(STANDARD, 0) + 1
            resp = await self._model_chat(
                [
                    {"role": "system", "content": "你只输出 JSON 工具名数组。"},
                    {"role": "user", "content": prompt},
                ],
                None,
                tier=STANDARD,
            )
            names = self._parse_tool_names(resp.content)
        except BudgetExceeded:
            # 预算熔断不是"聚焦失败"，必须继续向上冒泡到统一熔断点，不得回退全量。
            raise
        except Exception as exc:  # noqa: BLE001 - 聚焦是优化不是依赖，失败回退全量
            self._diag(
                DiagnosticKind.SCHEMA_FOCUS_FALLBACK,
                f"工具 schema 聚焦失败（{type(exc).__name__}），回退注入全量 schema",
                level=DiagnosticLevel.DEBUG,
                error=f"{type(exc).__name__}: {exc}",
            )
            return self.registry.llm_schemas(), None
        chosen: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name in seen or not self.registry.has(name):
                continue
            seen.add(name)
            chosen.append(name)
        if not chosen:
            return self.registry.llm_schemas(), None
        # meta-tool（replan/request_capability/create_code_tool）始终注入，
        # 聚焦裁剪只针对业务工具，避免计划走不通时看不到重规划入口。
        return self._with_meta_schemas(self.registry.schemas_for(chosen)), chosen

    @staticmethod
    def _parse_tool_names(content: str) -> list[str]:
        """从模型输出里容错提取工具名 JSON 数组；解析失败返回空列表（触发回退）。"""
        match = re.search(r"\[.*\]", content, flags=re.DOTALL)
        if match is None:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        return [str(x).strip() for x in data if isinstance(x, str) and str(x).strip()]

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
    ) -> AsyncIterator[AgentEvent]:
        """核心 ReAct 循环：模型调用 ↔ 工具执行，直到模型不再调工具或触达迭代上限。

        每轮先做上下文压缩（若启用）与成本感知选档（档位相对上轮变化才发事件），
        再调用模型；模型不再发起工具调用即产出最终消息并结束。否则顺序执行本轮
        工具——内核 meta-tool 直接处理、普通工具走 :class:`ToolExecutor`——把失败
        观察回灌促其自我修正，并把执行中新注册工具的完整 schema 并入下一轮
        （"发现了就能调到"）。达到 ``max_iters`` 后要求模型基于已有观察收尾。
        ``use_tools=False``（DIRECT）时不注入任何工具。
        """
        if use_tools:
            tools_schema, focused_names = await self._focus_tool_schemas(task, tier_counts)
        else:
            tools_schema, focused_names = None, None
        # 跨轮状态：是否已发生工具失败（决定纠偏升档）、上一轮档位（仅变化时发事件）。
        had_failure = False
        last_tier: str | None = None
        # 重规划次数（经 meta 调用链累加），用于 plan_revised 事件的修订序号。
        replanned: list[int] = [0]

        for iteration in range(self.max_iters):
            # 摘要压缩：超高水位时把旧轮压成摘要（最近完整轮保留），避免长对话
            # 硬裁剪丢失早期事实；压缩本身可观测，模型失败回退硬裁剪。
            if self.compaction_policy is not None:
                compact_ev = await compact_context(
                    ctx,
                    self.compaction_model or self.model,
                    self.compaction_policy,
                    timeout=self.model_timeout,
                    on_diagnostic=self._on_diagnostic,
                )
                if compact_ev is not None:
                    yield compact_ev
            # 成本·延迟感知选档：默认 standard 省钱，失败纠偏/后段卡住才升 strong；
            # 关闭策略时恒 standard（旧行为）。档位相对上一轮变化时发事件，不啰嗦。
            if tier_policy is not None:
                tier_dec = tier_policy.select(
                    strategy=strategy,
                    iteration=iteration,
                    had_failure=had_failure,
                )
                tier = tier_dec.tier
                tier_reason = tier_dec.reason
            else:
                tier = STANDARD
                tier_reason = "成本感知关闭，执行轮用标准档"
            if tier != last_tier:
                yield AgentEvent(
                    EventType.TIER_CHANGED,
                    {"tier": tier, "reason": tier_reason, "iteration": iteration},
                )
            last_tier = tier
            if tier_counts is not None:
                tier_counts[tier] = tier_counts.get(tier, 0) + 1
            try:
                resp = await self._model_chat(self._model_messages(ctx), tools_schema, tier=tier)
            except BudgetExceeded:
                # 预算熔断冒泡到 astream 统一收尾，不能被下面的"模型失败体面 return"吞掉。
                raise
            except Exception as exc:  # noqa: BLE001
                is_timeout = isinstance(exc, TimeoutError)
                self._diag(
                    DiagnosticKind.MODEL_TIMEOUT if is_timeout else DiagnosticKind.MODEL_ERROR,
                    (
                        f"执行轮模型调用超过 {self.model_timeout:g}s 硬超时，已终止"
                        if is_timeout
                        else f"执行轮模型调用失败（{type(exc).__name__}），已终止本轮"
                    ),
                    level=DiagnosticLevel.ERROR,
                    stage="react",
                    iteration=iteration,
                    error=f"{type(exc).__name__}: {exc}",
                )
                yield AgentEvent(EventType.ERROR, {"error": f"{type(exc).__name__}: {exc}"})
                return

            if not resp.tool_calls:
                text = resp.content.strip()
                ctx.add(ChatMessage(role="assistant", content=text))
                yield AgentEvent(EventType.MODEL_MESSAGE, {"text": text, "tier": tier})
                return

            self._append_assistant_tool_calls(ctx, resp)
            failed: list[str] = []
            newly_registered: list[str] = []
            async for ev in self._execute_tool_calls(
                resp.tool_calls,
                ctx,
                task,
                failed,
                newly_registered,
                replanned,
                tier_policy,
                tier_counts,
                subject,
            ):
                yield ev

            # 执行中发现的新工具：下一轮 function-calling 必须能看到其完整 schema，
            # 否则模型"发现了却调不到"。全量模式重算；聚焦模式把新名字并入聚焦集。
            if newly_registered and use_tools:
                tools_schema = self._schema_after_registration(newly_registered, focused_names)

            if failed:
                # 轻量反思（不额外调用模型）：失败观察回灌成明确引导，让模型自我修正；
                # 同时置失败标记，下一轮选档据此升 strong。
                had_failure = True
                self._append_failure_reflection(ctx, failed)

        # 达到迭代上限：要求模型基于已有观察收尾。
        async for ev in self._finish_at_iteration_limit(ctx, tier_counts):
            yield ev

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
            # 业务工具执行前过预算硬墙（meta-tool 编排不计入工具次数）；
            # 超限直接冒泡到 astream 统一熔断，该工具不会被执行、不产生副作用。
            if self._active_budget is not None:
                self._active_budget.before_tool_call()
            tool_events, ok, result_text = await self.executor.execute(
                call.name, call.arguments, subject=subject
            )
            if self._active_budget is not None:
                self._active_budget.record_tool_call()
            for tool_event in tool_events:
                yield tool_event
            ctx.add(
                ChatMessage(
                    role="tool",
                    content=result_text,
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
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
        self, ctx: Context, tier_counts: dict[str, int] | None = None
    ) -> AsyncIterator[AgentEvent]:
        """达到工具调用上限：要求模型基于已有观察直接收尾。"""
        ctx.add(
            ChatMessage(
                role="user",
                content="已达到工具调用上限，请基于已有结果直接给出最终答案。",
            )
        )
        if tier_counts is not None:
            tier_counts[STANDARD] = tier_counts.get(STANDARD, 0) + 1
        resp = await self._model_chat(self._model_messages(ctx), None, tier=STANDARD)
        yield AgentEvent(
            EventType.MODEL_MESSAGE,
            {"text": resp.content.strip(), "tier": STANDARD},
        )

    async def _handle_meta_tool(
        self,
        call: ToolCallRequest,
        ctx: Context,
        task: str,
        replanned: list[int] | None = None,
        tier_policy: TierPolicy | None = None,
        tier_counts: dict[str, int] | None = None,
        subject: Subject | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """分发内核 meta-tool；未知 meta-tool 优雅降级为工具结果错误。"""
        if call.name == REQUEST_CAPABILITY:
            async for ev in self._handle_request_capability(call, ctx, task, subject):
                yield ev
            return
        if call.name == CREATE_CODE_TOOL:
            async for ev in self._handle_create_code_tool(call, ctx, subject):
                yield ev
            return
        if call.name == REPLAN:
            async for ev in self._handle_replan(
                call, ctx, task, replanned, tier_policy, tier_counts
            ):
                yield ev
            return
        # 理论上不可达（meta_tools 集合由内核控制），防御性回灌而非崩溃。
        ctx.add(
            ChatMessage(
                role="tool",
                content=f"内核不认识的 meta-tool：{call.name}",
                tool_call_id=call.id,
                name=call.name,
            )
        )

    async def _handle_request_capability(
        self,
        call: ToolCallRequest,
        ctx: Context,
        task: str,
        subject: Subject | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """执行中"请求发现新能力"：授权 -> 发缺口事件 -> 发现注册 -> 回灌结果。"""
        # meta-tool 同样先快照：审批看到的与内核实际执行的必须是同一份参数
        arguments = (
            snapshot_arguments(call.arguments)
            if isinstance(call.arguments, dict)
            else {}
        )
        auth_events, approved, reason = await authorize_tool_call(
            self.executor.policy,
            self.channel,
            REQUEST_CAPABILITY,
            arguments,
            subject=subject,
        )
        for ev in auth_events:
            yield ev

        def _reply(content: str) -> None:
            ctx.add(
                ChatMessage(
                    role="tool",
                    content=content,
                    tool_call_id=call.id,
                    name=call.name,
                )
            )

        if not approved:
            _reply(f"能力请求未被授权：{reason}。请基于现有工具给出结论或说明限制。")
            return
        need = str(arguments.get("need", "")).strip()
        if not need:
            _reply("缺少 need 参数：请用一句话描述你缺失但完成任务必需的能力。")
            return

        # 执行中能力缺口（区别于路由阶段，phase=react）。
        yield AgentEvent(
            EventType.CAPABILITY_MISSING,
            {
                "task": task,
                "missing": need,
                "available_tools": [s.name for s in self.registry.all()],
                "phase": "react",
            },
        )
        if self.discovery is None:
            _reply("宿主未配置能力发现源，无法发现新工具；请如实告知用户当前能力不足。")
            return

        before = {s.name for s in self.registry.all()}
        async for ev in self._discover_tools(need, task):
            yield ev
        registered = sorted({s.name for s in self.registry.all()} - before)
        if registered:
            _reply(
                "已发现并注册新工具："
                + ", ".join(registered)
                + "。请在下一轮直接调用其中合适的工具完成任务，不要重复请求发现。"
            )
        else:
            _reply(
                "未在宿主能力目录中找到匹配该需求的工具。请如实告知用户当前缺少该能力，"
                "不要编造工具或数据。"
            )

    async def _handle_create_code_tool(
        self,
        call: ToolCallRequest,
        ctx: Context,
        subject: Subject | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """执行中创建代码工具：授权 -> 校验 -> 注册（TTL 生命周期）-> 回灌结果。

        创建是敏感动作，除"无需审批"档外都要先授权；真正执行该工具时
        ToolExecutor 还会再走一次权限闸（创建与首次执行双重授权）。
        """
        # meta-tool 同样先快照：审批看到的与内核实际执行的必须是同一份参数
        arguments = (
            snapshot_arguments(call.arguments)
            if isinstance(call.arguments, dict)
            else {}
        )
        auth_events, approved, reason = await authorize_tool_call(
            self.executor.policy,
            self.channel,
            CREATE_CODE_TOOL,
            arguments,
            subject=subject,
        )
        for ev in auth_events:
            yield ev

        def _reply(content: str) -> None:
            ctx.add(
                ChatMessage(
                    role="tool",
                    content=content,
                    tool_call_id=call.id,
                    name=call.name,
                )
            )

        if not approved:
            _reply(f"创建代码工具未被授权：{reason}。请改用现有工具或组合工具。")
            return
        manager = self.executor.code_manager
        if manager is None:
            _reply("宿主未提供代码沙箱，无法创建代码工具；请改用组合工具 compose_tool。")
            return

        name = str(arguments.get("name", "")).strip()
        description = str(arguments.get("description", "")).strip()
        code = str(arguments.get("code", "")).strip()
        input_schema = arguments.get("input_schema")
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}
        try:
            spec = manager.create(name, description, input_schema, code)
        except ValueError as exc:
            _reply(f"代码工具创建失败：{exc}")
            return

        yield AgentEvent(
            EventType.CODE_TOOL_CREATED,
            {
                "name": spec.name,
                "source": "code",
                "ttl_seconds": manager.default_ttl,
                "permanent": False,
            },
        )
        _reply(
            f"已创建代码工具 {spec.name}（默认 {manager.default_ttl // 3600} 小时有效，"
            "每次被调用都会刷新存活期；后台可将其设为永久保留）。请在下一轮直接调用它，"
            "首次执行仍会按权限策略确认，且代码在宿主隔离沙箱中运行。"
        )

    async def _handle_replan(
        self,
        call: ToolCallRequest,
        ctx: Context,
        task: str,
        replanned: list[int] | None,
        tier_policy: TierPolicy | None,
        tier_counts: dict[str, int] | None,
    ) -> AsyncIterator[AgentEvent]:
        """执行中重规划：标注调整 -> 产出修正计划 -> 回灌继续（不走授权闸）。

        replan 只重排内部计划、不产生外部副作用，因此不要求用户再授权；
        内核保留已完成步骤、基于当前观察产出修正计划，并发 ``plan_revised`` 事件。
        """
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        reason = str(arguments.get("reason", "")).strip() or "执行中发现原计划需要调整"
        revision = 1
        if replanned is not None:
            replanned[0] += 1
            revision = replanned[0]
        # 重规划属于复杂拆解：成本感知开启时由策略给档（默认 strong），否则 strong。
        plan_tier = tier_policy.for_planning().tier if tier_policy is not None else STRONG
        ctx.add(
            ChatMessage(
                role="user",
                content=(
                    "【系统重规划】原计划执行到此因“" + reason + "”需要调整。"
                    "请保留已经拿到结果、无需重做的步骤，基于当前已获得的观察，"
                    "重新输出 2-5 步编号计划（每行一个步骤，用 1. 2. 编号），"
                    "不要重复已完成步骤；只输出步骤本身。"
                ),
            )
        )
        if tier_counts is not None:
            tier_counts[plan_tier] = tier_counts.get(plan_tier, 0) + 1
        resp = await self._model_chat(self._model_messages(ctx), None, tier=plan_tier)
        steps = [s.strip() for s in re.findall(r"\d+[.、)]\s*(.+)", resp.content)]
        if not steps:
            steps = [task]
        plan_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
        ctx.add(ChatMessage(role="assistant", content=plan_text))
        yield AgentEvent(
            EventType.PLAN_REVISED,
            {"revision": revision, "reason": reason, "steps": steps},
        )
        # 回灌 tool 结果：模型下一轮按新计划继续执行。
        ctx.add(
            ChatMessage(
                role="tool",
                content=(
                    "计划已按当前观察修订（第 " + str(revision) + " 版）。"
                    "请按新计划继续调用工具执行，不要重复已完成步骤，"
                    "拿到全部结果后给出最终汇报。"
                ),
                tool_call_id=call.id,
                name=call.name,
            )
        )
