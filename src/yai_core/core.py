"""AgentCore 门面：宿主接入的唯一入口。

from yai_core import AgentCore
core = AgentCore.auto(my_app)          # 内省宿主能力，自动注册工具
result = await core.run("整理本周笔记")  # 自适应选策略/工具/模型
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Literal

from yai_core.channels import CollectChannel
from yai_core.diagnostics import DiagnosticSink
from yai_core.discovery import discover
from yai_core.kernel import (
    DEFAULT_MODEL_TIMEOUT,
    AdaptiveRouter,
    AgentLoop,
    CompactionPolicy,
    TierPolicy,
)
from yai_core.learning import RouteOutcome, extract_route_outcome
from yai_core.memory import InMemoryStore, MemoryVault, build_memory_tools
from yai_core.policy import AllowlistPolicy
from yai_core.spi import (
    Channel,
    MemoryStore,
    ModelProvider,
    PermissionPolicy,
    RouteSelector,
    ToolDiscovery,
    ToolSandbox,
)
from yai_core.tools import (
    CREATE_CODE_TOOL,
    REQUEST_CAPABILITY,
    ToolExecutor,
    ToolRegistry,
    build_composer_tool,
    build_create_code_tool,
    build_request_capability_tool,
)
from yai_core.tools.code_tools import CodeToolManager
from yai_core.types import AgentEvent, RunResult, Strategy, ToolSpec

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
        sandbox: ToolSandbox | None = None,
        code_storage: Path | str | None = None,
        long_memory: bool = False,
        memory_storage: Path | str | None = None,
        max_iters: int = 6,
        max_clarify_rounds: int = 2,
        full_schema_budget: int = 24,
        learning: RouteSelector | None = None,
        on_feedback: FeedbackHook | None = None,
        context_compaction: bool | CompactionPolicy = False,
        cost_aware: bool = True,
        model_timeout: float | None = DEFAULT_MODEL_TIMEOUT,
        on_diagnostic: DiagnosticSink | None = None,
    ) -> None:
        # 实例级任务串行锁：astream 全程持有，保证一个 core 内任务不在 await 点交错。
        self._task_lock = asyncio.Lock()
        self.model = model
        self.registry = ToolRegistry()
        # 自校准路由学习器（第六个 SPI）：默认 None，行为与不接入学习完全一致。
        self.learning = learning
        self.on_feedback = on_feedback
        self.max_iters = max_iters
        self.max_clarify_rounds = max_clarify_rounds
        self.channel = channel or CollectChannel()
        self.memory = memory or InMemoryStore()
        self.policy = policy or AllowlistPolicy(mode="allow_all" if auto_approve_tools else "auto")
        # 路由模型注入：
        # - False（默认）：永不 LLM 分类，零额外模型调用、离线完全确定；
        # - True：总是 LLM 分类，失败回退规则；
        # - "auto"：仅当模型后端自报 yai_live_router 标记时才分类，
        #   离线脚本模型自动走规则，examples 与线上可统一传 "auto"。
        if router is not None:
            self.router = router
        else:
            if llm_router is True:
                route_model: ModelProvider | None = model
            elif llm_router == "auto":
                route_model = model if getattr(model, "yai_live_router", False) else None
            elif llm_router is False:
                route_model = None
            else:
                raise ValueError('llm_router 只接受 True、False 或 "auto"')
            self.router = AdaptiveRouter(model=route_model, selector=learning)
        # 外部传入的 router 若支持 selector 属性，也挂上学习器（唯一前置点）。
        if learning is not None and hasattr(self.router, "selector"):
            self.router.selector = learning
        # 代码工具能力：仅当宿主提供沙箱时启用。内核不内置执行器，
        # 只做注册表 + TTL 生命周期；没有沙箱就不注册 create_code_tool。
        self.code_manager: CodeToolManager | None = None
        if sandbox is not None:
            self.code_manager = CodeToolManager(self.registry, storage_path=code_storage)
        # 结构化长期记忆：opt-in。vault 负责 remember/recall 的存储与词法检索，
        # 内核不内置向量长期记忆（更强语义召回由宿主经 Embedding SPI 提供）。
        self.memory_vault: MemoryVault | None = None
        if long_memory:
            self.memory_vault = MemoryVault(storage_path=memory_storage)
        self.executor = ToolExecutor(
            self.registry,
            self.policy,
            self.channel,
            sandbox=sandbox,
            code_manager=self.code_manager,
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
            meta_tools=meta_tools,
            # 上下文摘要压缩：opt-in，默认关闭；灵动岛真机开启。
            compaction_policy=comp_policy,
            # 成本·延迟感知选档：默认开启（该花才花）；cost_aware=False 时恒 standard。
            tier_policy=TierPolicy() if cost_aware else None,
            # 单次模型调用硬超时（秒）；None=关闭，交给模型客户端自身超时。
            model_timeout=model_timeout,
            # 本地诊断回调（旁路排障），并由 AgentLoop 鸭子接线给 router/model。
            on_diagnostic=on_diagnostic,
        )
        self.cost_aware = cost_aware
        self.model_timeout = model_timeout

    # ---------- 接入 ----------

    @classmethod
    def auto(cls, host: object, model: ModelProvider, **kwargs: object) -> AgentCore:
        """内省宿主模块/对象，自动发现并注册全部公开能力。"""
        core = cls(model, **kwargs)  # type: ignore[arg-type]
        core.register_tools(discover(host))
        return core

    def register_tools(self, specs: list[ToolSpec]) -> None:
        self.registry.register_many(specs)

    def list_tools(self) -> list[dict]:
        return [
            {"name": s.name, "description": s.description, "source": s.source}
            for s in self.registry.all()
        ]

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

    # ---------- 运行 ----------

    async def astream(self, task: str) -> AsyncIterator[AgentEvent]:
        """运行一个任务并流式产出事件。

        并发契约：实例内任意时刻只运行一个任务。并发调用 ``astream`` /
        :meth:`run` 会在实例锁上排队，而不会让两条任务在 await 点交错、互相
        污染共享的上下文、记忆、工具注册表与路由学习态。要并发处理多条相互
        独立的会话，请为每条会话实例化独立的 :class:`AgentCore`。

        流式宿主若提前结束消费（``break`` 或丢弃迭代器），异步生成器被关闭时
        会自动释放该锁；请正常耗尽或显式关闭事件流，否则后续任务会一直等待。
        """
        # 流式宿主（网页/终端）也走这里，因此反馈闭环必须在 astream 收尾闭合，
        # 而不能只放在 run() 里——否则只消费事件流的宿主永远不会回灌学习。
        async with self._task_lock:
            events: list[AgentEvent] = []
            strategy: Strategy | None = None
            completed = False
            try:
                async for event in self._loop.astream(task):
                    await self.channel.emit(event)
                    events.append(event)
                    data = event.data or {}
                    if "strategy" in data:
                        strategy = Strategy(data["strategy"])
                    yield event
                completed = True
            finally:
                # 仅在任务正常跑完时回灌；被取消（GeneratorExit/CancelledError）不计入，
                # 避免把"用户中途终止"误当成负样本污染学习。
                if completed:
                    chosen = strategy or self.router.classify(task, self.registry)
                    self._record_feedback(task, chosen, events)

    async def run(self, task: str) -> RunResult:
        events: list[AgentEvent] = []
        final_text = ""
        strategy: Strategy | None = None
        async for event in self.astream(task):
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
