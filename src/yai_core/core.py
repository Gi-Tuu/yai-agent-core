"""AgentCore 门面：宿主接入的唯一入口。

    from yai_core import AgentCore
    core = AgentCore.auto(my_app)          # 内省宿主能力，自动注册工具
    result = await core.run("整理本周笔记")  # 自适应选策略/工具/模型
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Literal

from yai_core.channels import CollectChannel
from yai_core.discovery import discover
from yai_core.kernel import AdaptiveRouter, AgentLoop
from yai_core.learning import RouteOutcome, extract_route_outcome
from yai_core.memory import InMemoryStore
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
        max_iters: int = 6,
        max_clarify_rounds: int = 2,
        full_schema_budget: int = 24,
        learning: RouteSelector | None = None,
        on_feedback: FeedbackHook | None = None,
    ) -> None:
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
                route_model = (
                    model if getattr(model, "yai_live_router", False) else None
                )
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
            self.code_manager = CodeToolManager(self.registry)
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
        # 执行中动态发现：只有配置了发现源，才注册 request_capability，
        # 避免给模型一个注定无法兑现的工具。它由 Agent Loop 直接拦截处理。
        if discovery is not None:
            self.registry.register(build_request_capability_tool())
            meta_tools.add(REQUEST_CAPABILITY)
        # 代码工具创建：宿主提供沙箱时才注册，由 Agent Loop 拦截创建动作。
        if sandbox is not None:
            self.registry.register(build_create_code_tool())
            meta_tools.add(CREATE_CODE_TOOL)
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
        )

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

    # ---------- 运行 ----------

    async def astream(self, task: str) -> AsyncIterator[AgentEvent]:
        async for event in self._loop.astream(task):
            await self.channel.emit(event)
            yield event

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
        result = RunResult(
            strategy=chosen,
            events=events,
            final_text=final_text,
        )
        # 反馈闭环：任务结束后从事件流抽取结果并回灌学习器（无学习器即 no-op）。
        self._record_feedback(task, chosen, events)
        return result

    def _record_feedback(
        self, task: str, chosen: Strategy, events: list[AgentEvent]
    ) -> None:
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
