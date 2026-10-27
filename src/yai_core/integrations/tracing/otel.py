"""结构化 tracing：把一次 Agent run 转成一条 OpenTelemetry trace（可选 extra）。

设计红线（与内核零硬依赖一致）：

- **外侧订阅，不侵入 loop**：本模块只消费 :meth:`AgentCore.astream` 已经 yield 出来的
  :class:`~yai_core.types.AgentEvent`，事件如何产生完全在 ``kernel/`` 内部，这里不改一行。
- **可选依赖、懒加载**：``opentelemetry`` 只在 ``otel`` extra 里，模块顶层不 import；
  内核本体 ``dependencies`` 仍为空。未安装时调用会给出明确的安装提示。
- **只做 instrumentation library**：本模块不初始化 TracerProvider / Processor / Exporter，
  那是宿主进程的职责（OTel 惯例）。宿主配置好后端（OTLP/Jaeger/Tempo/内存）后，把
  ``tracer`` 传进来，或直接用全局 ``trace.get_tracer(...)``。

span 模型：

- 一次 run = 一个 root span（``yai.agent.run``），记录任务、最终策略、模型档位、结果长度；
- 每次工具调用 = root 下的子 span（``yai.tool.<name>``），``TOOL_CALL`` 开、``TOOL_RESULT``
  关，成功记 ``ok=true`` 与结果预览，失败置 span 状态为 ERROR 并记错误；
- 其余自适应事件（策略选择 / 计划 / 澄清 / 能力缺口 / 发现·组合·创建工具 / 压缩 / 重规划 /
  档位切换 / 权限询问 / 错误）记为 root span 上的 span event 或属性。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from yai_core.types import AgentEvent, EventType, RunResult, Strategy

#: root span 名：一次完整的 Agent run。
ROOT_SPAN_NAME = "yai.agent.run"
#: 工具子 span 名前缀，实际名为 ``yai.tool.<工具名>``。
TOOL_SPAN_PREFIX = "yai.tool"
#: 写入 span 的字符串属性上限，超出截断，防止超大参数 / 预览把 trace 撑爆。
_ATTR_LIMIT = 2000
#: 不直接塞进 span event 的事件字段（体积大或已单独成属性）。
_EVENT_SKIP_KEYS = frozenset({"final_text"})

#: OpenTelemetry GenAI 语义约定中与本内核相关的最小属性集（agent / tool 子集）。
#: 只发这几个稳定字段，不臆造 usage / model 等我们拿不到或不稳定的数据。
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_TYPE = "gen_ai.tool.type"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
#: 本内核在 trace 里对外呈现的 agent 名。
AGENT_NAME = "yai-agent-core"


def _load_otel(tracer: Any | None) -> tuple[Any, Any]:
    """懒加载 OpenTelemetry API，返回 ``(trace 模块, tracer 实例)``。

    未安装 ``otel`` extra 时抛出带安装指引的 :class:`ImportError`；``tracer`` 为 None
    时回退到全局 tracer provider（OTel 在未配置时返回 no-op tracer，不会上报）。
    """
    try:
        from opentelemetry import trace
    except ImportError as exc:  # pragma: no cover - 取决于宿主是否安装 extra
        raise ImportError(
            "结构化 tracing 需要可选依赖 otel，请先安装：pip install "
            '"yai-agent-core[otel]"（内核本体保持零硬依赖）。'
        ) from exc
    if tracer is None:
        tracer = trace.get_tracer("yai-agent-core")
    return trace, tracer


def _attr_value(value: Any) -> str | bool | int | float:
    """把任意事件字段转成 OTel 属性接受的标量类型，超长截断。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - default=str 下的极端防御
            text = repr(value)  # pragma: no cover
    if len(text) <= _ATTR_LIMIT:
        return text
    return text[:_ATTR_LIMIT] + "…(truncated)"


class _RunRecorder:
    """把单个 run 的事件流映射到一条 trace（root span + 工具子 span）。

    工具是严格顺序执行的（执行器逐个 await），``TOOL_CALL`` 与 ``TOOL_RESULT`` 不会
    交错，因此用一个栈按工具名配对即可；组合工具 / meta-tool 不发 ``TOOL_CALL``，
    遇到没有对应开 span 的 ``TOOL_RESULT`` 时补一个即时子 span，保证不丢观测。
    """

    def __init__(self, trace_mod: Any, tracer: Any, root_attributes: dict[str, Any] | None):
        self._trace = trace_mod
        self._tracer = tracer
        self._root: Any = None
        self._attach: Any = None
        self._open_tools: list[tuple[str, Any]] = []
        self._failed = False
        self._root_attributes = root_attributes or {}

    def start(self, task: str) -> None:
        self._root = self._tracer.start_span(ROOT_SPAN_NAME)
        # 把 root 挂为当前 span（不自动 end），使后续 start_span 的父节点指向 root。
        self._attach = self._trace.use_span(self._root, end_on_exit=False)
        self._attach.__enter__()
        self._root.set_attribute("yai.task", _attr_value(task))
        # GenAI 语义约定：让 Jaeger / Grafana 等后端按标准识别"这是一次 agent 调用"。
        self._root.set_attribute(GEN_AI_OPERATION_NAME, "invoke_agent")
        self._root.set_attribute(GEN_AI_AGENT_NAME, AGENT_NAME)
        for key, value in self._root_attributes.items():
            self._root.set_attribute(str(key), _attr_value(value))

    def on_event(self, event: AgentEvent) -> None:
        etype = event.type
        data = event.data or {}
        if etype == EventType.TOOL_CALL:
            self._on_tool_call(data)
        elif etype == EventType.TOOL_RESULT:
            self._on_tool_result(data)
        else:
            self._on_lifecycle_event(etype, data)

    def _on_tool_call(self, data: dict[str, Any]) -> None:
        name = str(data.get("tool", "unknown"))
        span = self._tracer.start_span(f"{TOOL_SPAN_PREFIX}.{name}")
        span.set_attribute("yai.tool", name)
        span.set_attribute(GEN_AI_TOOL_NAME, name)
        span.set_attribute(GEN_AI_TOOL_TYPE, "function")
        if "arguments" in data:
            span.set_attribute("yai.arguments", _attr_value(data["arguments"]))
        self._open_tools.append((name, span))

    def _on_tool_result(self, data: dict[str, Any]) -> None:
        name = str(data.get("tool", "unknown"))
        span = self._pop_open(name)
        if span is None:
            # 无配对 TOOL_CALL（组合工具 / 内核 meta-tool 的结果）：补即时子 span。
            span = self._tracer.start_span(f"{TOOL_SPAN_PREFIX}.{name}")
            span.set_attribute("yai.tool", name)
            span.set_attribute(GEN_AI_TOOL_NAME, name)
            span.set_attribute(GEN_AI_TOOL_TYPE, "function")
            span.set_attribute("yai.without_call_event", True)
        receipt = data.get("receipt")
        if isinstance(receipt, dict) and receipt.get("call_id"):
            span.set_attribute(GEN_AI_TOOL_CALL_ID, str(receipt["call_id"]))
        ok = bool(data.get("ok", True))
        span.set_attribute("yai.ok", ok)
        if ok:
            if "preview" in data:
                span.set_attribute("yai.preview", _attr_value(data["preview"]))
        else:
            error = str(data.get("error", "tool failed"))
            span.set_attribute("yai.error", _attr_value(error))
            span.set_status(self._trace.StatusCode.ERROR, error)
        span.end()

    def _pop_open(self, name: str) -> Any:
        """弹出与工具名匹配的、最近的未闭合 span（顺序执行下通常就是栈顶）。"""
        for index in range(len(self._open_tools) - 1, -1, -1):
            if self._open_tools[index][0] == name:
                return self._open_tools.pop(index)[1]
        return None

    def _on_lifecycle_event(self, etype: EventType, data: dict[str, Any]) -> None:
        if etype == EventType.STRATEGY_SELECTED and "strategy" in data:
            self._root.set_attribute("yai.strategy", str(data["strategy"]))
        if etype == EventType.TIER_CHANGED and "tier" in data:
            self._root.set_attribute("yai.tier", str(data["tier"]))
        if etype == EventType.DONE:
            self._root.set_attribute(
                "yai.final_text_len", len(str(data.get("final_text", "")))
            )
            return
        if etype == EventType.ERROR:
            message = str(data.get("error") or _attr_value(data))
            self._failed = True
            self._root.set_status(self._trace.StatusCode.ERROR, message)
        attributes = {
            f"yai.{key}": _attr_value(value)
            for key, value in data.items()
            if key not in _EVENT_SKIP_KEYS
        }
        self._root.add_event(f"yai.{etype.value}", attributes=attributes)

    def finish(self, error: BaseException | None) -> None:
        # 容错：流被取消 / 异常中断时，先关掉所有未闭合的工具子 span。
        for _name, span in reversed(self._open_tools):
            span.set_attribute("yai.unclosed", True)
            span.end()
        self._open_tools.clear()
        if error is not None:
            self._root.record_exception(error)
            self._root.set_status(
                self._trace.StatusCode.ERROR, f"{type(error).__name__}: {error}"
            )
        elif not self._failed:
            self._root.set_status(self._trace.StatusCode.OK)
        self._attach.__exit__(None, None, None)
        self._root.end()


async def traced_astream(
    core: Any,
    task: str,
    *,
    tracer: Any | None = None,
    root_attributes: dict[str, Any] | None = None,
) -> AsyncIterator[AgentEvent]:
    """像 :meth:`AgentCore.astream` 一样逐事件产出，同时在外侧记录一条 trace。

    宿主侧用法（边渲染边埋点，事件一个不丢）::

        async for event in traced_astream(core, "整理本周笔记"):
            render(event)

    - ``tracer``：可选的 OpenTelemetry tracer；不传则用全局 provider（未配置时是 no-op）。
    - ``root_attributes``：附加到 root span 的静态属性（如宿主名、版本、会话 id）。
    """
    trace_mod, otel_tracer = _load_otel(tracer)
    recorder = _RunRecorder(trace_mod, otel_tracer, root_attributes)
    recorder.start(task)
    error: BaseException | None = None
    try:
        async for event in core.astream(task):
            recorder.on_event(event)
            yield event
    except BaseException as exc:  # noqa: BLE001 - 必须在关闭 span 后原样上抛
        error = exc
        raise
    finally:
        recorder.finish(error)


async def traced_run(
    core: Any,
    task: str,
    *,
    tracer: Any | None = None,
    root_attributes: dict[str, Any] | None = None,
) -> RunResult:
    """带 trace 的 :meth:`AgentCore.run`：消费整条流并聚合为 :class:`RunResult`。"""
    events: list[AgentEvent] = []
    final_text = ""
    strategy: Strategy | None = None
    async for event in traced_astream(
        core, task, tracer=tracer, root_attributes=root_attributes
    ):
        events.append(event)
        data = event.data
        if "strategy" in data:
            try:
                strategy = Strategy(data["strategy"])
            except ValueError:  # pragma: no cover - 内核只发合法 Strategy
                strategy = None  # pragma: no cover
        if event.type == EventType.DONE:
            final_text = str(data.get("final_text", ""))
    if strategy is None:  # pragma: no cover - 正常 run 必发 STRATEGY_SELECTED
        strategy = core.router.classify(task, core.registry)  # pragma: no cover
    return RunResult(strategy=strategy, events=events, final_text=final_text)
