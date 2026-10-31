"""OpenTelemetry 结构化 tracing 测试。

全部离线：用 SDK 的 InMemorySpanExporter 在进程内断言 span 树，不连任何 collector、
不需要 API Key。未安装 otel extra 时，需要 SDK 的用例自动 skip；"未安装即给出安装
提示"的用例始终运行（被测模块本身懒加载，顶层不依赖 opentelemetry）。
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from yai_core import (
    AgentCore,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    build_spec,
)
from yai_core.integrations.tracing import ROOT_SPAN_NAME, traced_astream, traced_run
from yai_core.integrations.tracing.otel import (
    AGENT_NAME,
    GEN_AI_AGENT_NAME,
    GEN_AI_OPERATION_NAME,
    GEN_AI_TOOL_CALL_ID,
    GEN_AI_TOOL_NAME,
    GEN_AI_TOOL_TYPE,
    TOOL_SPAN_PREFIX,
)


def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


def boom() -> str:
    """总是失败的工具。"""
    raise RuntimeError("boom exploded")


def _make_tracer():
    """构造进程内 TracerProvider + 内存导出器，返回 (provider, exporter, tracer)。"""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter, provider.get_tracer("yai-test")


async def _collect(core, task, tracer):
    events = []
    async for event in traced_astream(core, task, tracer=tracer):
        events.append(event)
    return events


def test_missing_extra_raises_with_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """未安装 opentelemetry 时，开始记录 trace 要给出 pip install [otel] 提示。"""
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    from yai_core.integrations.tracing.otel import _load_otel

    with pytest.raises(ImportError, match="yai-agent-core"):
        _load_otel(None)


def test_trace_maps_run_to_root_and_tool_child_spans() -> None:
    """react 运行：一个 root span + 每个工具一个子 span，成功/失败状态正确、父子相连。"""
    provider, exporter, tracer = _make_tracer()
    from opentelemetry.trace import INVALID_SPAN_CONTEXT, StatusCode

    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="1", name="add", arguments={"a": 1, "b": 2}),
                    ToolCallRequest(id="2", name="boom", arguments={}),
                ],
            ),
            ModelResponse(content="结果是 3，但 boom 失败了"),
        ]
    )
    core = AgentCore(model, auto_approve_tools=True)
    core.register_tools([build_spec(add), build_spec(boom)])

    try:
        events = asyncio.run(_collect(core, "请计算这两个数并检查 boom", tracer))
        provider.force_flush()
        spans = exporter.get_finished_spans()
    finally:
        provider.shutdown()

    by_name = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(span)

    # 恰好一个 root span，且为真正的根（无父 span）。
    roots = by_name[ROOT_SPAN_NAME]
    assert len(roots) == 1
    root = roots[0]
    assert root.parent is None or root.parent == INVALID_SPAN_CONTEXT
    assert root.attributes["yai.strategy"] == "react"
    # GenAI 语义约定：agent 侧的标准字段。
    assert root.attributes[GEN_AI_OPERATION_NAME] == "invoke_agent"
    assert root.attributes[GEN_AI_AGENT_NAME] == AGENT_NAME
    assert root.attributes["yai.final_text_len"] == len("结果是 3，但 boom 失败了")
    assert root.status.status_code == StatusCode.OK

    # 两个工具子 span，父节点都是 root。
    add_span = by_name[f"{TOOL_SPAN_PREFIX}.add"][0]
    boom_span = by_name[f"{TOOL_SPAN_PREFIX}.boom"][0]
    assert add_span.parent.span_id == root.context.span_id
    assert boom_span.parent.span_id == root.context.span_id
    assert add_span.attributes["yai.ok"] is True
    # GenAI 语义约定：工具侧的标准字段（含调用 id，来自执行收据）。
    assert add_span.attributes[GEN_AI_TOOL_NAME] == "add"
    assert add_span.attributes[GEN_AI_TOOL_TYPE] == "function"
    assert add_span.attributes[GEN_AI_TOOL_CALL_ID] == "1"
    assert add_span.attributes["yai.preview"] == "3"
    assert boom_span.attributes["yai.ok"] is False
    assert "boom exploded" in boom_span.attributes["yai.error"]
    assert boom_span.status.status_code == StatusCode.ERROR

    # 事件转发不丢：两个调用、两个结果、一个 DONE。
    types = [e.type.value for e in events]
    assert types.count("tool_call") == 2
    assert types.count("tool_result") == 2
    assert types[-1] == "done"


def test_trace_marks_root_error_when_stream_raises() -> None:
    """事件流本身抛异常时：root span 置 ERROR、记录异常，并把异常原样上抛。"""
    provider, exporter, tracer = _make_tracer()
    from opentelemetry.trace import StatusCode

    class ExplodingCore:
        async def astream(self, task: str):  # noqa: ARG002
            raise RuntimeError("core exploded")
            yield  # 使其成为 async generator  # pragma: no cover

    try:
        with pytest.raises(RuntimeError, match="core exploded"):
            asyncio.run(_collect(ExplodingCore(), "任意任务", tracer))
        provider.force_flush()
        spans = exporter.get_finished_spans()
    finally:
        provider.shutdown()

    root = [s for s in spans if s.name == ROOT_SPAN_NAME][0]
    assert root.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in root.events)


def test_traced_run_direct_without_tools_has_only_root_span() -> None:
    """direct 直答（无工具调用）：只有 root span，traced_run 正常聚合 RunResult。"""
    provider, exporter, tracer = _make_tracer()
    from opentelemetry.trace import StatusCode

    core = AgentCore(
        ScriptedModel([ModelResponse(content="你好，我是内嵌助手。")]),
        auto_approve_tools=True,
    )
    try:
        result = asyncio.run(traced_run(core, "你好", tracer=tracer))
        provider.force_flush()
        spans = exporter.get_finished_spans()
    finally:
        provider.shutdown()

    assert result.final_text == "你好，我是内嵌助手。"
    tool_spans = [s for s in spans if s.name.startswith(TOOL_SPAN_PREFIX)]
    assert tool_spans == []
    root = [s for s in spans if s.name == ROOT_SPAN_NAME][0]
    assert root.status.status_code == StatusCode.OK


def test_attr_value_coerces_scalars_json_and_truncation() -> None:
    """属性值转换：标量原样、容器转 JSON、超长截断。"""
    from yai_core.integrations.tracing.otel import _ATTR_LIMIT, _attr_value

    assert _attr_value(True) is True
    assert _attr_value(7) == 7
    assert _attr_value(2.5) == 2.5
    assert _attr_value({"a": 1}) == '{"a": 1}'
    assert _attr_value([1, 2]) == "[1, 2]"
    long_value = _attr_value("x" * (_ATTR_LIMIT + 50))
    assert isinstance(long_value, str)
    assert long_value.endswith("…(truncated)")


def _make_recorder(tracer, root_attributes=None):
    from yai_core.integrations.tracing.otel import _load_otel, _RunRecorder

    trace_mod, _ = _load_otel(tracer)
    return _RunRecorder(trace_mod, tracer, root_attributes)


def test_orphan_result_unclosed_span_and_root_attributes() -> None:
    """无 TOOL_CALL 的结果补即时 span；流中断时未闭合工具 span 被收尾；root 属性透传。"""
    from yai_core.types import AgentEvent, EventType

    provider, exporter, tracer = _make_tracer()
    try:
        recorder = _make_recorder(tracer, {"yai.host": "notes"})
        recorder.start("任意任务")
        # 组合工具只发 TOOL_RESULT（无 TOOL_CALL）：应补一个带标记的即时子 span。
        recorder.on_event(
            AgentEvent(
                EventType.TOOL_RESULT,
                {"tool": "morning_brief", "ok": True, "preview": "done"},
            )
        )
        # 开了工具 span 却没有结果（模拟流被取消 / 异常中断）。
        recorder.on_event(
            AgentEvent(EventType.TOOL_CALL, {"tool": "add", "arguments": {"a": 1}})
        )
        recorder.finish(None)
        provider.force_flush()
        spans = exporter.get_finished_spans()
    finally:
        provider.shutdown()

    by_name = {span.name: span for span in spans}
    root = by_name[ROOT_SPAN_NAME]
    assert root.attributes["yai.host"] == "notes"
    orphan = by_name[f"{TOOL_SPAN_PREFIX}.morning_brief"]
    assert orphan.attributes["yai.without_call_event"] is True
    assert orphan.attributes["yai.ok"] is True
    unclosed = by_name[f"{TOOL_SPAN_PREFIX}.add"]
    assert unclosed.attributes["yai.unclosed"] is True


def test_error_event_marks_root_failed_and_is_not_overridden() -> None:
    """ERROR 事件把 root 置为失败，finish 时不得被默认 OK 覆盖。"""
    from yai_core.types import AgentEvent, EventType

    provider, exporter, tracer = _make_tracer()
    from opentelemetry.trace import StatusCode

    try:
        recorder = _make_recorder(tracer)
        recorder.start("t")
        recorder.on_event(AgentEvent(EventType.ERROR, {"error": "model blew up"}))
        recorder.finish(None)
        provider.force_flush()
        root = [s for s in exporter.get_finished_spans() if s.name == ROOT_SPAN_NAME][0]
    finally:
        provider.shutdown()

    assert root.status.status_code == StatusCode.ERROR
    assert "model blew up" in (root.status.description or "")


def test_load_otel_without_explicit_tracer_uses_global() -> None:
    """不传 tracer 时回退全局 tracer provider（未配置则为 no-op），不报错。"""
    pytest.importorskip("opentelemetry.sdk")
    from yai_core.integrations.tracing.otel import _load_otel

    trace_mod, tracer = _load_otel(None)
    assert tracer is not None
    assert hasattr(trace_mod, "StatusCode")


def test_otel_span_attributes_do_not_leak_sensitive_arguments() -> None:
    """B7 审计 B-4：OTel span 属性不得携带敏感工具参数明文。"""
    from yai_core import build_spec
    from yai_core.channels import CollectChannel
    from yai_core.policy import AllowlistPolicy
    from yai_core.tools import ToolExecutor, ToolRegistry
    from yai_core.types import ToolGovernance

    async def transfer_money(amount: int, api_key: str = "") -> str:
        """转账。"""
        return "ok"

    spec = build_spec(transfer_money)
    spec.governance = ToolGovernance(sensitive=("api_key",))
    registry = ToolRegistry()
    registry.register_many([spec])
    executor = ToolExecutor(
        registry,
        AllowlistPolicy(("transfer_money",), mode="allow_all"),
        CollectChannel(),
    )
    events, ok, _ = asyncio.run(
        executor.execute(
            "transfer_money",
            {"amount": 100, "api_key": "offline-placeholder-key"},
            call_id="c1",
        )
    )
    assert ok
    serialized = json.dumps([e.data for e in events], ensure_ascii=False, default=str)
    assert "offline-placeholder-key" not in serialized, (
        "敏感参数（已声明 sensitive）不得以明文进入任何事件属性"
    )
