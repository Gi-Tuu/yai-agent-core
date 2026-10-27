"""AgentCore 门面：宿主接入的唯一入口。

from yai_core import AgentCore
core = AgentCore.auto(my_app)          # 内省宿主能力，自动注册工具
result = await core.run("整理本周笔记")  # 自适应选策略/工具/模型
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

from yai_core.channels import CollectChannel
from yai_core.config import CoreConfig
from yai_core.diagnostics import DiagnosticSink
from yai_core.discovery import discover
from yai_core.kernel import (
    DEFAULT_MODEL_TIMEOUT,
    AdaptiveRouter,
    AgentLoop,
    Budget,
    CompactionPolicy,
    HostStateRegistry,
    TierPolicy,
)
from yai_core.kernel.state import HostStateProvider
from yai_core.learning import RouteOutcome, extract_route_outcome
from yai_core.memory import InMemoryStore, MemoryVault, build_memory_tools
from yai_core.metrics import MetricsRecorder
from yai_core.policy import AllowlistPolicy
from yai_core.spi import (
    Channel,
    EmbeddingProvider,
    MemoryStore,
    ModelProvider,
    PermissionPolicy,
    RouteSelector,
    StorageCipher,
    ToolDiscovery,
    ToolSandbox,
)
from yai_core.tools import (
    CREATE_CODE_TOOL,
    REQUEST_CAPABILITY,
    CapabilityHealth,
    ToolExecutor,
    ToolRegistry,
    build_composer_tool,
    build_create_code_tool,
    build_request_capability_tool,
    check_tool_specs,
)
from yai_core.tools.code_tools import CodeToolManager
from yai_core.tools.executor import DEFAULT_TOOL_RESULT_CHARS
from yai_core.types import AgentEvent, EventType, RunResult, Strategy, Subject, ToolSpec

#: 任务结束后把"原始任务 + 实际策略 + 事后结果"回调给宿主（用于防抖落盘/埋点）。
FeedbackHook = Callable[[str, Strategy, RouteOutcome], None]


class AgentCore:
    """进程内嵌入式自适应 Agent 内核的唯一门面。

    并发模型：一个实例代表一条**串行会话**，实例内同一时刻只运行一个任务；
    需要并发会话（多租户 / 多专员 / 多子员工）时，请为每条会话实例化独立的
    ``AgentCore``（可共享无状态的模型 / 发现源 / 沙箱单例）。详见
    :meth:`astream` 的并发契约说明。
    """

    def __init__(
        self,
        model: ModelProvider,
        *,
        channel: Channel | None = None,
        memory: MemoryStore | None = None,
        policy: PermissionPolicy | None = None,
        router: AdaptiveRouter | None = None,
        auto_approve_tools: bool = True,
        llm_router: bool | Literal["auto"] = False,
        discovery: ToolDiscovery | None = None,
        composition: bool = False,
        strict_direct: bool = False,
        sandbox: ToolSandbox | None = None,
        code_storage: Path | str | None = None,
        long_memory: bool = False,
        memory_storage: Path | str | None = None,
        memory_cipher: StorageCipher | None = None,
        memory_embedder: EmbeddingProvider | None = None,
        auto_memory_recall: bool = False,
        auto_recall_k: int = 3,
        max_iters: int = 6,
        max_clarify_rounds: int = 2,
        full_schema_budget: int = 24,
        tool_result_char_limit: int | None = DEFAULT_TOOL_RESULT_CHARS,
        learning: RouteSelector | None = None,
        on_feedback: FeedbackHook | None = None,
        context_compaction: bool | CompactionPolicy = False,
        compaction_model: ModelProvider | None = None,
        cost_aware: bool = True,
        model_timeout: float | None = DEFAULT_MODEL_TIMEOUT,
        tool_timeout: float | None = None,
        on_diagnostic: DiagnosticSink | None = None,
        tool_output_guard: bool = True,
        budget: Budget | None = None,
        metrics: MetricsRecorder | None = None,
    ) -> None:
        # 装配早期统一校验：非法取值（迭代次数 / 预算类型 / 压缩策略 / 超时等）
        # 在这里就抛出，而不是等到任务运行中途。
        CoreConfig(
            channel=channel,
            memory=memory,
            policy=policy,
            router=router,
            discovery=discovery,
            sandbox=sandbox,
            learning=learning,
            on_diagnostic=on_diagnostic,
            on_feedback=on_feedback,
            metrics=metrics,
            auto_approve_tools=auto_approve_tools,
            llm_router=llm_router,
            composition=composition,
            strict_direct=strict_direct,
            code_storage=code_storage,
            long_memory=long_memory,
            memory_storage=memory_storage,
            memory_cipher=memory_cipher,
            memory_embedder=memory_embedder,
            auto_memory_recall=auto_memory_recall,
            auto_recall_k=auto_recall_k,
            max_iters=max_iters,
            max_clarify_rounds=max_clarify_rounds,
            full_schema_budget=full_schema_budget,
            tool_result_char_limit=tool_result_char_limit,
            context_compaction=context_compaction,
            cost_aware=cost_aware,
            model_timeout=model_timeout,
            tool_timeout=tool_timeout,
            budget=budget,
        ).validate()
        # 实例级任务串行锁：astream 全程持有，保证一个 core 内任务不在 await 点交错。
        self._task_lock = asyncio.Lock()
        self.model = model
        self.registry = ToolRegistry()
        # 自校准路由学习器（RouteSelector 契约）：默认 None，行为与不接入学习完全一致。
        self.learning = learning
        self.on_feedback = on_feedback
        self.max_iters = max_iters
        self.max_clarify_rounds = max_clarify_rounds
        # 没传通道就用收集型默认实现。`CollectChannel` 满足基础 Channel 契约
        # （两参数 confirm）；审批上下文是可选扩展，不再靠 cast 把类型不兼容藏起来。
        self.channel = channel or CollectChannel()
        self.memory = memory or InMemoryStore()
        self.discovery = discovery
        self.policy = policy or AllowlistPolicy(mode="allow_all" if auto_approve_tools else "auto")
        # 路由模型注入：
        # - False（默认）：永不 LLM 分类，零额外模型调用、离线完全确定；
        # - True：总是 LLM 分类，失败回退规则；
        # - "auto"：仅当模型后端自报 yai_live_router 标记时才分类，
        #   离线脚本模型自动走规则，examples 与线上可统一传 "auto"。
        self.router = self._resolve_router(
            model, router=router, llm_router=llm_router, learning=learning
        )
        # 代码工具能力：仅当宿主提供沙箱时启用。内核不内置执行器，
        # 只做注册表 + TTL 生命周期；没有沙箱就不注册 create_code_tool。
        self.code_manager: CodeToolManager | None = None
        if sandbox is not None:
            self.code_manager = CodeToolManager(self.registry, storage_path=code_storage)
        # 结构化长期记忆：opt-in。vault 负责 remember/recall 的存储与词法检索，
        # 内核不内置向量长期记忆（更强语义召回由宿主经 Embedding SPI 提供）。
        self.memory_vault: MemoryVault | None = None
        if long_memory:
            # memory_cipher 提供时对落盘记忆做静态加密（内存中仍为明文）；
            # 不传则明文 JSON（默认），是否加密/密钥来源由宿主决定。
            self.memory_vault = MemoryVault(
                storage_path=memory_storage,
                cipher=memory_cipher,
                embedder=memory_embedder,
            )
        self.executor = ToolExecutor(
            self.registry,
            self.policy,
            self.channel,
            sandbox=sandbox,
            code_manager=self.code_manager,
            result_char_limit=tool_result_char_limit,
            handler_timeout=tool_timeout,
        )
        meta_tools: set[str] = set()
        # 组合工具能力：显式开启后注册 compose_tool meta-tool，模型可在运行时
        # 把宿主已注册的工具编排成新工具（只能引用已注册工具，每步仍走权限）。
        if composition:
            self.registry.register(build_composer_tool(self.registry))
        # 长期记忆工具：普通 native 工具（非 meta），走 executor 统一权限闸。
        if self.memory_vault is not None:
            self.registry.register_many(build_memory_tools(self.memory_vault))
        # 执行中动态发现：只有配置了发现源，才注册 request_capability，
        # 避免给模型一个注定无法兑现的工具。它由 Agent Loop 直接拦截处理。
        if discovery is not None:
            self.registry.register(build_request_capability_tool())
            meta_tools.add(REQUEST_CAPABILITY)
        # 代码工具创建：宿主提供沙箱时才注册，由 Agent Loop 拦截创建动作。
        if sandbox is not None:
            self.registry.register(build_create_code_tool())
            meta_tools.add(CREATE_CODE_TOOL)
        # 上下文摘要压缩：False 关闭（零额外模型调用）；True 用默认策略；
        # 传入 CompactionPolicy 则用自定义水位（测试 / 宿主可调）。
        if context_compaction is True:
            comp_policy: CompactionPolicy | None = CompactionPolicy()
        elif isinstance(context_compaction, CompactionPolicy):
            comp_policy = context_compaction
        elif context_compaction is False:
            comp_policy = None
        else:
            raise ValueError("context_compaction 只接受 False、True 或 CompactionPolicy")
        # 宿主实时状态（现状锚）：注册表与 loop 共享同一对象，宿主可在构造后
        # 随时注册/注销；未注册时零开销、零行为变化。
        self.host_state = HostStateRegistry()
        self._loop = AgentLoop(
            model=self.model,
            registry=self.registry,
            executor=self.executor,
            channel=self.channel,
            memory=self.memory,
            router=self.router,
            discovery=discovery,
            max_iters=max_iters,
            max_clarify_rounds=max_clarify_rounds,
            full_schema_budget=full_schema_budget,
            strict_direct=strict_direct,
            meta_tools=meta_tools,
            # 上下文摘要压缩：opt-in，默认关闭；灵动岛真机开启。
            compaction_policy=comp_policy,
            # 摘要专用模型：None 时压缩走主模型；可注入更便宜的小模型省钱。
            compaction_model=compaction_model,
            # 成本·延迟感知选档：默认开启（该花才花）；cost_aware=False 时恒 standard。
            tier_policy=TierPolicy() if cost_aware else None,
            # 单次模型调用硬超时（秒）；None=关闭，交给模型客户端自身超时。
            model_timeout=model_timeout,
            # 本地诊断回调（旁路排障），并由 AgentLoop 鸭子接线给 router/model。
            on_diagnostic=on_diagnostic,
            # 任务级预算硬上限（模型/工具/token），None=不限制；防止失控烧钱。
            budget=budget,
            # 任务起始主动召回：把 vault.arecall 绑定 k 后作为回调交给 loop；
            # 未开启长期记忆或未开 auto_memory_recall 时为 None（loop 行为不变）。
            auto_recall=(
                functools.partial(self.memory_vault.arecall, k=auto_recall_k)
                if (auto_memory_recall and self.memory_vault is not None)
                else None
            ),
            auto_recall_k=auto_recall_k,
            host_state=self.host_state,
            tool_output_guard=tool_output_guard,
        )
        self.cost_aware = cost_aware
        self.model_timeout = model_timeout
        self.budget = budget
        # 运行指标收集器：默认零依赖收集（成本/健康可观测）；宿主可注入共享或自定义实现。
        self.metrics = metrics or MetricsRecorder()
        # **本实例自己创建**、因此由本实例负责收尾的资源。宿主传进来的对象
        # 默认不在这里——多个 core 常共享同一个模型 / 记忆 / 发现源，替别人关是误伤。
        self._owned: list[Any] = []
        if channel is None:
            self._owned.append(self.channel)
        if memory is None:
            self._owned.append(self.memory)
        if self.memory_vault is not None:
            self._owned.append(self.memory_vault)
        self._closed = False
        self._task_subject: Subject | None = None

    # ---------- 生命周期 ----------

    async def aclose(self, *, close_shared: bool = False) -> None:
        """收尾：等在跑的任务结束后，关闭可关闭的资源。幂等，重复调用无副作用。

        默认只关**本实例自建**的资源（未传 channel/memory 时的默认实现、
        ``long_memory=True`` 时内核创建的记忆库）。``model`` / ``memory`` /
        ``discovery`` / ``sandbox`` / ``channel`` 这些宿主传进来的对象**默认不动**。
        进程退出前确实要连整张图一起收，传 ``close_shared=True``。

        顺序固定为 channel → discovery → sandbox → memory → model：先断外部能力，
        再断记忆与模型。任何单个资源关闭失败都不阻止其余收尾，但会在**全部尝试之后**
        抛出汇总异常——收尾问题要能被运维看见，不能静默吞掉。

        注意：本方法持有实例串行锁等待当前任务，若任务已卡死，请先取消它。
        """
        targets: list[Any] = list(self._owned)
        if close_shared:
            for candidate in (
                self.channel,
                self.discovery,
                self.executor.sandbox,
                self.memory,
                self.model,
            ):
                if candidate is not None and all(candidate is not seen for seen in targets):
                    targets.append(candidate)
        failures: list[str] = []
        async with self._task_lock:
            if self._closed:
                return
            for resource in targets:
                closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
                if closer is None:
                    continue
                try:
                    outcome = closer()
                    if inspect.isawaitable(outcome):
                        await outcome
                except Exception as exc:  # noqa: BLE001 - 一个关不掉不能挡住其余收尾
                    failures.append(f"{type(resource).__name__}: {exc}")
            self._closed = True
        if failures:
            raise RuntimeError("收尾时以下资源关闭失败：" + "；".join(failures))

    # ---------- 装配辅助 ----------

    @staticmethod
    def _resolve_router(
        model: ModelProvider,
        *,
        router: AdaptiveRouter | None,
        llm_router: bool | Literal["auto"],
        learning: RouteSelector | None,
    ) -> AdaptiveRouter:
        """决定路由实例：显式传入优先；否则按 llm_router 开关构造，并挂学习器。"""
        if router is not None:
            resolved = router
        else:
            if llm_router is True:
                route_model: ModelProvider | None = model
            elif llm_router == "auto":
                route_model = model if getattr(model, "yai_live_router", False) else None
            elif llm_router is False:
                route_model = None
            else:
                raise ValueError('llm_router 只接受 True、False 或 "auto"')
            resolved = AdaptiveRouter(model=route_model, selector=learning)
        # 外部传入的 router 若支持 selector 属性，也挂上学习器（唯一前置点）。
        if learning is not None and hasattr(resolved, "selector"):
            resolved.selector = learning
        return resolved

    # ---------- 接入 ----------

    @classmethod
    def from_config(cls, model: ModelProvider, config: CoreConfig) -> AgentCore:
        """用一份 :class:`CoreConfig` + 一个模型实例构造 core（配置可在多处复用）。"""
        return cls(model, **config.validate().as_kwargs())

    @classmethod
    def auto(cls, host: object, model: ModelProvider, **kwargs: object) -> AgentCore:
        """内省宿主模块/对象，自动发现并注册全部公开能力。"""
        core = cls(model, **kwargs)  # type: ignore[arg-type]
        core.register_tools(discover(host))
        return core

    def register_tools(self, specs: list[ToolSpec]) -> None:
        self.registry.register_many(specs)

    # ---------- 宿主实时状态（现状锚） ----------

    def register_state_provider(
        self, name: str, provider: HostStateProvider, *, replace: bool = False
    ) -> None:
        """注册一个**只读**的宿主状态快照回调。

        回调无参，返回字符串 / JSON 值 / None（或它们的 awaitable）；内核在每次
        组装模型消息前调用它，把最新状态作为瞬态 system 块注入——不写入对话历史、
        不进压缩与长期记忆。返回值是数据不是指令，且只能由宿主注册：模型参数、
        工具结果与用户消息都改不了它。

        重名默认报错（避免静默覆盖别的状态源）；要替换请显式传 ``replace=True``。
        回调无参、状态属于本 core 实例：多租户请每会话一个 core，避免状态串号。
        """
        self.host_state.register(name, provider, replace=replace)

    def unregister_state_provider(self, name: str) -> bool:
        """注销一个状态源；返回是否真的存在并移除。"""
        return self.host_state.unregister(name)

    def state_provider_names(self) -> tuple[str, ...]:
        """当前已注册的状态源名称（注册顺序）。"""
        return self.host_state.names()

    def list_tools(self) -> list[dict]:
        return [
            {"name": s.name, "description": s.description, "source": s.source}
            for s in self.registry.all()
        ]

    def capability_health(self) -> CapabilityHealth:
        """对当前注册的工具做静态接入体检；不执行 handler、不发事件。"""
        return check_tool_specs(
            self.registry.all(),
            registry=self.registry,
            sandbox_available=self.executor.sandbox is not None,
        )

    # ---------- 代码工具生命周期（后台管理） ----------

    def retain_code_tool(self, name: str) -> bool:
        """把某个代码工具置为永久保留（后台白名单化）；未启用沙箱时返回 False。"""
        if self.code_manager is None:
            return False
        return self.code_manager.make_permanent(name)

    def code_tools_status(self) -> dict:
        """返回代码工具注册表状态（数量、存活、永久保留、逐项明细）。"""
        if self.code_manager is None:
            return {"enabled": False, "total": 0, "live": 0, "permanent": 0, "tools": []}
        return {"enabled": True, **self.code_manager.status()}

    def sweep_code_tools(self) -> list[str]:
        """回收所有过期且非永久的代码工具，返回被回收的工具名（宿主可定时调用）。"""
        if self.code_manager is None:
            return []
        return self.code_manager.expire_stale()

    # ---------- 长期记忆（后台管理） ----------

    def long_memory_status(self) -> dict:
        """返回长期记忆 vault 状态（总数、各类型计数、最近条目）；未启用返回 enabled=False。"""
        if self.memory_vault is None:
            return {"enabled": False, "total": 0}
        return {"enabled": True, **self.memory_vault.status()}

    def forget_memory(self, note_id: str) -> bool:
        """删除指定长期记忆；未启用或不存在返回 False。"""
        if self.memory_vault is None:
            return False
        return self.memory_vault.forget(note_id)

    # ---------- 运行指标（成本 / 健康可观测） ----------

    def metrics_snapshot(self) -> dict:
        """返回运行指标快照（runs / 工具 / 模型调用 / 事件计数 / 时延直方图）。"""
        return self.metrics.snapshot()

    def _metrics_start(self) -> None:
        """指标旁路：run 开始打点，任何异常都不得反噬主流程。"""
        try:
            self.metrics.on_run_start()
        except Exception:  # noqa: BLE001 - 与 diagnostics 旁路同口径
            pass

    def _metrics_event(self, event: AgentEvent) -> None:
        """指标旁路：逐事件累计，任何异常都不得反噬主流程。"""
        try:
            self.metrics.record_event(event)
        except Exception:  # noqa: BLE001 - 与 diagnostics 旁路同口径
            pass

    def _metrics_finish(self, exc: BaseException | None) -> None:
        """指标旁路：run 结束结算（含取消 / 失败分类），异常不外抛。"""
        try:
            self.metrics.on_run_finish(exc)
        except Exception:  # noqa: BLE001 - 与 diagnostics 旁路同口径
            pass

    # ---------- 运行 ----------

    async def astream(
        self, task: str, *, subject: Subject | None = None
    ) -> AsyncIterator[AgentEvent]:
        """运行一个任务并流式产出事件。

        并发契约：实例内任意时刻只运行一个任务。并发调用 ``astream`` /
        :meth:`run` 会在实例锁上排队，而不会让两条任务在 await 点交错、互相
        污染共享的上下文、记忆、工具注册表与路由学习态。要并发处理多条相互
        独立的会话，请为每条会话实例化独立的 :class:`AgentCore`。

        ``subject`` 是**宿主注入的可信操作主体**（``None`` = 本上下文没有主体）：
        它随本次运行透传到权限闸，模型无法通过输出或工具参数伪造它——想要冒充身份，
        只能拿到宿主进程本身。声明了 ``ToolGovernance.gate`` 的能力与
        ``yai_subject_aware`` 的策略会读到它，其余策略按原签名调用。

        流式宿主若提前结束消费（``break`` 或丢弃迭代器），异步生成器被关闭时
        会自动释放该锁；请正常耗尽或显式关闭事件流，否则后续任务会一直等待。

        DONE 是**展示终态**，不是生产者的最后动作：本次运行的学习回灌发生在
        DONE 之后。消费者收到 DONE 就 break 并关闭流，会跳过这次回灌；只把
        DONE 当结束的界面应继续耗尽事件流（后续事件可忽略）。
        """
        # 流式宿主（网页/终端）也走这里，因此反馈闭环必须在 astream 收尾闭合，
        # 而不能只放在 run() 里——否则只消费事件流的宿主永远不会回灌学习。
        async with self._task_lock:
            if self._closed:
                raise RuntimeError("AgentCore 已关闭，不能运行任务")
            events: list[AgentEvent] = []
            strategy: Strategy | None = None
            # 反馈要奖励**最先做出的路由决策**：澄清链里原任务先选 clarify，
            # 补充信息后才由内层重新路由成 react；只记最后一个策略会把功劳记错。
            decision_strategy: Strategy | None = None
            completed = False
            run_error: BaseException | None = None
            # 指标旁路：run 开始打点；任何异常都不允许反噬主流程。
            self._metrics_start()
            try:
                # 实例锁保证薄壳委派读取的主体属于当前任务，而非模型参数。
                self._task_subject = subject
                async for event in self._loop.astream(task, subject=subject):
                    await self.channel.emit(event)
                    events.append(event)
                    self._metrics_event(event)
                    data = event.data or {}
                    if "strategy" in data:
                        strategy = Strategy(data["strategy"])
                        if decision_strategy is None and event.type == EventType.STRATEGY_SELECTED:
                            decision_strategy = strategy
                    yield event
                completed = True
            except BaseException as exc:
                # 记录取消 / 失败后原样抛出（CancelledError/GeneratorExit 必须继续传播）。
                run_error = exc
                raise
            finally:
                self._task_subject = None
                self._metrics_finish(run_error)
                # 仅在任务正常跑完时回灌；被取消（GeneratorExit/CancelledError）不计入，
                # 避免把"用户中途终止"误当成负样本污染学习。
                if completed:
                    chosen = (
                        decision_strategy
                        or strategy
                        or self.router.classify(task, self.registry)
                    )
                    self._record_feedback(task, chosen, events)

    async def run(self, task: str, *, subject: Subject | None = None) -> RunResult:
        events: list[AgentEvent] = []
        final_text = ""
        strategy: Strategy | None = None
        async for event in self.astream(task, subject=subject):
            events.append(event)
            data = event.data
            if "strategy" in data:
                strategy = Strategy(data["strategy"])
            if event.type.value == "done":
                final_text = data.get("final_text", "")
        chosen = strategy or self.router.classify(task, self.registry)
        # 反馈回灌已在 astream() 正常收尾时完成，这里不再重复记录。
        return RunResult(
            strategy=chosen,
            events=events,
            final_text=final_text,
        )

    def _record_feedback(self, task: str, chosen: Strategy, events: list[AgentEvent]) -> None:
        """事后回灌：学习只发生在任务结束后，不影响当次稳定性。

        反馈不进当次事件流（事件流在收集完才算得出结果），通过 ``on_feedback``
        回调与 :meth:`route_learning_status` 暴露给宿主（方案 A）。
        """
        if self.learning is None:
            return
        outcome = extract_route_outcome(
            events,
            chosen,
            max_iters=self.max_iters,
            clarify_budget=self.max_clarify_rounds,
        )
        self.learning.record(task, self.registry, chosen, outcome)
        if self.on_feedback is not None:
            self.on_feedback(task, chosen, outcome)

    def route_learning_status(self) -> dict:
        """路由学习器的可观测状态；未启用返回 {"enabled": False}。"""
        if self.learning is None:
            return {"enabled": False}
        snapshot = getattr(self.learning, "to_dict", None)
        status: dict = {"enabled": True, "selector": type(self.learning).__name__}
        if callable(snapshot):
            status.update(snapshot())
        return status
