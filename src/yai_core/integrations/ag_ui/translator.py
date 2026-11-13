"""AG-UI 适配器：把 YAI 的唯一事件流翻译成 AG-UI 1.0 的 SSE 事件。

边界（与内核红线一致）：

- 本模块是**外侧订阅者**，不修改 ``kernel/`` / ``core.py`` 一行，也不新增事件流；
- 只依赖标准库：事件是普通 dict，SSE 编码是字符串拼接；
- 不是内置 UI：它只把已有事件翻译成标准前端协议，前端（CopilotKit 等）自己渲染。

映射口径见 ``docs/guides/ag-ui.md``。未映射的 YAI 事件统一变成 AG-UI ``CUSTOM``
事件（``name="yai.<event_type>"``），保证不丢信息、也不假装语义。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from yai_core.types import AgentEvent, EventType

#: AG-UI 1.0 event type 常量（wire 值）。
RUN_STARTED = "RUN_STARTED"
RUN_FINISHED = "RUN_FINISHED"
RUN_ERROR = "RUN_ERROR"
STEP_STARTED = "STEP_STARTED"
STEP_FINISHED = "STEP_FINISHED"
TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
TOOL_CALL_START = "TOOL_CALL_START"
TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
TOOL_CALL_END = "TOOL_CALL_END"
TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
CUSTOM = "CUSTOM"


def _event(event_type: str, **fields: Any) -> dict[str, Any]:
    return {"type": event_type, **fields}


def _type_value(event_type: EventType | str) -> str:
    return getattr(event_type, "value", str(event_type))


@dataclass
class AgUiTranslator:
    """一次 AG-UI run 的状态机：``start()`` → ``translate(event)*`` → ``finish()``。"""

    thread_id: str
    run_id: str
    _message_seq: int = field(default=0, init=False)
    _tool_seq: int = field(default=0, init=False)
    _tool_message_seq: int = field(default=0, init=False)
    #: 已发出 TOOL_CALL_START、等待 TOOL_CALL_RESULT 的调用：(call_id, tool_name)。
    _pending_tools: list[tuple[str, str]] = field(default_factory=list, init=False)
    _terminal: bool = field(default=False, init=False)
    _finished: bool = field(default=False, init=False)

    def start(self) -> list[dict[str, Any]]:
        """发出 RUN_STARTED；必须是最外层第一个事件。"""
        return [_event(RUN_STARTED, threadId=self.thread_id, runId=self.run_id)]

    def translate(self, event: AgentEvent) -> list[dict[str, Any]]:
        """把一个 YAI 事件翻译成 0..N 个 AG-UI 事件；终止后不再输出。"""
        if self._terminal:
            return []
        etype = event.type
        data = dict(event.data or {})
        if etype == EventType.MODEL_MESSAGE:
            return self._text_message(data)
        if etype == EventType.TOOL_CALL:
            return self._tool_call(data)
        if etype == EventType.TOOL_RESULT:
            return self._tool_result(data)
        if etype == EventType.DONE:
            self._terminal = True
            self._finished = True
            final_text = str(data.get("final_text") or "")
            fields: dict[str, Any] = {
                "threadId": self.thread_id,
                "runId": self.run_id,
            }
            if final_text:
                fields["result"] = final_text
            return [_event(RUN_FINISHED, **fields)]
        if etype == EventType.ERROR:
            self._terminal = True
            message = str(data.get("error") or data.get("message") or "agent run failed")
            fields = {"message": message}
            if data.get("stage"):
                fields["code"] = str(data["stage"])
            return [_event(RUN_ERROR, **fields)]
        # 其余 YAI 决策/生命周期事件：保留为 CUSTOM，前端可按需渲染/忽略。
        return [
            _event(
                CUSTOM,
                name=f"yai.{_type_value(etype)}",
                value=data,
            )
        ]

    def finish(self) -> list[dict[str, Any]]:
        """若模型/循环没有发 DONE，也补一个 RUN_FINISHED（幂等）。"""
        if self._terminal:
            return []
        self._terminal = True
        self._finished = True
        return [_event(RUN_FINISHED, threadId=self.thread_id, runId=self.run_id)]

    @property
    def terminal(self) -> bool:
        return self._terminal

    # ---------- 内部映射 ----------

    def _text_message(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        text = str(data.get("text") or "")
        if not text:
            return []
        # schema_focus 是内核内部提示，不当作面向用户的回答。
        if data.get("kind") == "schema_focus":
            return []
        self._message_seq += 1
        message_id = f"msg-{self._message_seq}"
        return [
            _event(TEXT_MESSAGE_START, messageId=message_id, role="assistant"),
            _event(TEXT_MESSAGE_CONTENT, messageId=message_id, delta=text),
            _event(TEXT_MESSAGE_END, messageId=message_id),
        ]

    def _tool_call(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        tool_name = str(data.get("tool") or "unknown")
        arguments = data.get("arguments") or {}
        self._tool_seq += 1
        call_id = f"call-{self._tool_seq}"
        self._pending_tools.append((call_id, tool_name))
        try:
            delta = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            delta = json.dumps(str(arguments), ensure_ascii=False)
        return [
            _event(TOOL_CALL_START, toolCallId=call_id, toolCallName=tool_name),
            _event(TOOL_CALL_ARGS, toolCallId=call_id, delta=delta),
            _event(TOOL_CALL_END, toolCallId=call_id),
        ]

    def _tool_result(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        tool_name = str(data.get("tool") or "unknown")
        call_id = self._pop_pending_tool(tool_name)
        self._tool_message_seq += 1
        message_id = f"tool-msg-{self._tool_message_seq}"
        if data.get("ok") is False:
            content = str(data.get("error") or "tool failed")
        else:
            content = str(data.get("preview") or data.get("result") or "")
        metadata: dict[str, Any] = {"ok": bool(data.get("ok", True))}
        if "replayed" in data:
            metadata["replayed"] = bool(data["replayed"])
        return [
            _event(
                TOOL_CALL_RESULT,
                messageId=message_id,
                toolCallId=call_id,
                content=content,
                role="tool",
                metadata=metadata,
            )
        ]

    def _pop_pending_tool(self, tool_name: str) -> str:
        for index in range(len(self._pending_tools) - 1, -1, -1):
            call_id, name = self._pending_tools[index]
            if name == tool_name:
                del self._pending_tools[index]
                return call_id
        # 没有对应 call（例如重放/事件顺序异常）：仍给出稳定 id，不丢结果。
        self._tool_seq += 1
        return f"call-{self._tool_seq}"


def encode_sse(event: dict[str, Any]) -> str:
    """把一个 AG-UI 事件编码成单条 SSE 帧（LF、单个 ``data:``、空行结尾）。

    AG-UI 1.0 HTTP+SSE binding 要求每个 ``data`` 恰好是一个完整 JSON 事件，
    生产者必须用 LF 行尾；消费者会忽略其他 SSE 字段。
    """
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n"


def stream_sse(events: Iterable[dict[str, Any]]) -> Iterator[str]:
    """把事件序列变成 SSE 字符串流（FastAPI ``StreamingResponse`` 可直接消费）。"""
    for event in events:
        yield encode_sse(event)
