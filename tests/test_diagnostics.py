"""P0-3：零依赖本地诊断 SPI，全部离线、零网络、零 API Key。

诊断事件承接"内核已静默兜底、主流程不失败、但排障必须看得见"的瞬间。
本测试覆盖：
1. emit 基础语义：None 回调安静运行、正常回调收到事件、回调抛错不反噬；
2. 模型硬超时 → model_timeout（ERROR）；
3. LLM 路由分类失败回退规则 → router_llm_fallback（WARNING）；
4. 工具发现 / 可发现目录拉取失败 → discovery_failed / discoverable_list_failed；
5. FallbackModelProvider 可重试失败降级 → model_fallback（WARNING）；
6. AgentCore 把同一回调鸭子注入 router 与 FallbackModelProvider；
7. 上下文摘要压缩失败回退硬裁剪 → compaction_fallback。
"""

import asyncio

from yai_core import (
    AdaptiveRouter,
    AgentCore,
    DiagnosticEvent,
    DiagnosticKind,
    DiagnosticLevel,
    FallbackModelProvider,
    ModelResponse,
    ToolRegistry,
    ToolSpec,
)
from yai_core.diagnostics import emit
from yai_core.kernel import CompactionPolicy
from yai_core.kernel.compaction import compact_context
from yai_core.kernel.context import Context
from yai_core.types import ChatMessage

# ---------- 测试用假组件 ----------


class _SleepModel:
    """固定延迟后成功，用于触发硬超时。"""

    def __init__(self, delay: float = 1.0) -> None:
        self.delay = delay

    async def achat(self, messages, tools=None, *, tier="standard"):
        await asyncio.sleep(self.delay)
        return ModelResponse(content="不应到达")


class _BoomModel:
    """每次调用都抛通用错误（路由分类失败 / 压缩失败用）。"""

    model = "boom-model"

    async def achat(self, messages, tools=None, *, tier="standard"):
        raise RuntimeError("model boom")


class _ConnErrorModel:
    """抛连接错误（对 FallbackModelProvider 属于可重试降级的错误）。"""

    model = "flaky-model"

    async def achat(self, messages, tools=None, *, tier="standard"):
        raise ConnectionError("temporarily unreachable")


class _OkModel:
    model = "steady-model"

    async def achat(self, messages, tools=None, *, tier="standard"):
        return ModelResponse(content="兜底成功")


class _BrokenDiscovery:
    """discover 与 list_discoverable 都抛错的发现源。"""

    async def discover(self, need, *, task=None, available=None):
        raise RuntimeError("discovery source down")

    def list_discoverable(self):
        raise RuntimeError("catalog endpoint down")


def _registry_with_one_tool() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="ping",
            description="一个占位工具，让路由进入有工具分支",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: "pong",
        )
    )
    return registry


def _collect():
    """返回 (sink, events)；sink 把收到的诊断事件追加进 events。"""
    events: list[DiagnosticEvent] = []

    def sink(event: DiagnosticEvent) -> None:
        events.append(event)

    return sink, events


# ---------- 1. emit 基础语义 ----------


def test_emit_none_sink_is_noop() -> None:
    emit(None, DiagnosticEvent(kind="x", message="m"))  # 不应抛错


def test_emit_delivers_event() -> None:
    sink, events = _collect()
    ev = DiagnosticEvent(kind=DiagnosticKind.MODEL_ERROR, message="boom")
    emit(sink, ev)
    assert events == [ev]


def test_emit_swallows_sink_exception() -> None:
    def bad_sink(_event: DiagnosticEvent) -> None:
        raise RuntimeError("日志系统挂了")

    # 诊断是旁路，回调自身炸了绝不能反噬内核主流程。
    emit(bad_sink, DiagnosticEvent(kind="x", message="m"))


# ---------- 2. 模型硬超时 ----------


def test_model_timeout_emits_error_diagnostic() -> None:
    async def scenario() -> list[DiagnosticEvent]:
        sink, events = _collect()
        core = AgentCore(_SleepModel(delay=1.0), model_timeout=0.05, on_diagnostic=sink)
        await core.run("你好")
        return events

    events = asyncio.run(scenario())
    timeouts = [e for e in events if e.kind == DiagnosticKind.MODEL_TIMEOUT]
    assert len(timeouts) == 1
    assert timeouts[0].level == DiagnosticLevel.ERROR
    assert "超时" in timeouts[0].message
    # 无工具的直答任务在 react 主轮发起模型调用，超时由主轮局部捕获，stage=react。
    assert timeouts[0].data.get("stage") == "react"


# ---------- 3. LLM 路由回退 ----------


def test_router_llm_fallback_emits_warning() -> None:
    async def scenario() -> list[DiagnosticEvent]:
        sink, events = _collect()
        router = AdaptiveRouter(model=_BoomModel(), on_diagnostic=sink)
        decision = await router.aclassify("帮我查一下数据", _registry_with_one_tool())
        # 回退后仍给出确定性规则策略，任务不崩。
        assert decision.source == "rules"
        return events

    events = asyncio.run(scenario())
    fallbacks = [e for e in events if e.kind == DiagnosticKind.ROUTER_LLM_FALLBACK]
    assert len(fallbacks) == 1
    assert fallbacks[0].level == DiagnosticLevel.WARNING
    assert "RuntimeError" in fallbacks[0].data.get("error", "")


# ---------- 4. 工具发现失败 ----------


def test_discovery_failures_emit_diagnostics() -> None:
    async def scenario() -> list[DiagnosticEvent]:
        sink, events = _collect()
        core = AgentCore(_OkModel(), discovery=_BrokenDiscovery(), on_diagnostic=sink)
        # 可发现目录拉取失败（同步路径）。
        assert core._loop._discoverable_catalog() is None
        # 实际发现失败（async generator 路径）。
        collected = []
        async for ev in core._loop._discover_tools("需要天气能力", "查天气"):
            collected.append(ev)
        assert collected  # 至少发了一条 ERROR 业务事件
        return events

    events = asyncio.run(scenario())
    kinds = {e.kind for e in events}
    assert DiagnosticKind.DISCOVERABLE_LIST_FAILED in kinds
    assert DiagnosticKind.DISCOVERY_FAILED in kinds
    disc = next(e for e in events if e.kind == DiagnosticKind.DISCOVERY_FAILED)
    assert disc.level == DiagnosticLevel.ERROR
    assert disc.data.get("need") == "需要天气能力"


# ---------- 5. 多模型降级 ----------


def test_fallback_model_provider_emits_on_degrade() -> None:
    async def scenario() -> list[DiagnosticEvent]:
        sink, events = _collect()
        provider = FallbackModelProvider(
            [_ConnErrorModel(), _OkModel()],
            per_call_timeout=5.0,
            on_diagnostic=sink,
        )
        resp = await provider.achat([{"role": "user", "content": "hi"}])
        assert resp.content == "兜底成功"
        return events

    events = asyncio.run(scenario())
    degraded = [e for e in events if e.kind == DiagnosticKind.MODEL_FALLBACK]
    assert len(degraded) == 1
    assert degraded[0].level == DiagnosticLevel.WARNING
    assert degraded[0].data.get("failed_model") == "flaky-model"
    assert "ConnectionError" in degraded[0].data.get("error", "")


# ---------- 6. AgentCore 鸭子注入同一回调 ----------


def test_core_ducktypes_sink_into_router_and_model() -> None:
    sink, _ = _collect()
    provider = FallbackModelProvider([_OkModel()], on_diagnostic=None)
    core = AgentCore(provider, on_diagnostic=sink)
    # 路由（内部构造的 AdaptiveRouter）与模型后端都被注入同一回调。
    assert core._loop.router.on_diagnostic is sink
    assert core._loop.model.on_diagnostic is sink


def test_core_without_sink_leaves_components_none() -> None:
    provider = FallbackModelProvider([_OkModel()])
    core = AgentCore(provider)
    assert core._loop.router.on_diagnostic is None
    assert core._loop.model.on_diagnostic is None


# ---------- 7. 上下文压缩失败回退 ----------


def test_compaction_failure_emits_and_hard_trims() -> None:
    async def scenario() -> tuple[object, list[DiagnosticEvent], int]:
        sink, events = _collect()
        ctx = Context("s", max_chars=260)
        for i in range(4):
            ctx.messages += [
                ChatMessage(role="user", content=f"r{i}-用户-" + "U" * 120),
                ChatMessage(role="assistant", content=f"r{i}-助手-" + "A" * 120),
            ]
        before = len(ctx.messages)
        policy = CompactionPolicy(hard_max_chars=10, keep_recent_rounds=2)
        ev = await compact_context(ctx, _BoomModel(), policy, on_diagnostic=sink)
        return ev, events, before

    ev, events, before = asyncio.run(scenario())
    assert ev is None  # 压缩失败 → 回退硬裁剪，返回 None
    fallbacks = [e for e in events if e.kind == DiagnosticKind.COMPACTION_FALLBACK]
    assert len(fallbacks) == 1
    assert "RuntimeError" in fallbacks[0].message
