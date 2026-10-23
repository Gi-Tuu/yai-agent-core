"""模型把工具调用写成正文时的失败关闭：不展示伪调用、不执行工具。"""

from __future__ import annotations

import asyncio

from yai_core import AgentCore, ModelResponse, ScriptedModel, build_spec
from yai_core.types import EventType


def _core(content: str):
    def log_mood(mood: str, note: str = "") -> dict:
        """记录心情。"""
        return {"ok": True}

    model = ScriptedModel([ModelResponse(content=content)])
    core = AgentCore(model)
    core.register_tools([build_spec(log_mood)])
    return core


def test_raw_xml_tool_call_is_not_shown_or_executed() -> None:
    raw = (
        "用户表达今天还算开心。根据行为指引，我需要使用 log_mood 工具记录。\n"
        "<tool_call> <arg_key>message</arg_key> <arg_value>今天还算开心</arg_value>"
        " <arg_key>mood</arg_key> <arg_value>开心</arg_value> </tool_call>"
    )
    core = _core(raw)

    result = asyncio.run(core.run("我今天还算开心"))

    assert "<tool_call>" not in result.final_text
    assert "未识别的工具调用文本" in result.final_text
    assert not any(e.type == EventType.TOOL_CALL for e in result.events)
    assert not any(e.type == EventType.TOOL_RESULT for e in result.events)
    error = next(e for e in result.events if e.type == EventType.ERROR)
    assert error.data["stage"] == "model_protocol"
    assert error.data["raw_tool_markup"] is True
    # 上下文里也不能留下原始伪调用，避免下一轮模型继续复制。
    assert all("<tool_call>" not in m.content for m in core.memory.history())


def test_function_call_markup_is_also_rejected() -> None:
    raw = '<function_call name="log_mood">{"mood": "开心"}</function_call>'
    core = _core(raw)
    result = asyncio.run(core.run("今天开心"))
    assert "未识别的工具调用文本" in result.final_text
    assert not any(e.type == EventType.TOOL_CALL for e in result.events)


def test_mentioning_the_tag_without_call_shape_is_allowed() -> None:
    core = _core("`<tool_call>` 是一种常见的工具调用标记，但这里只是解释。")
    result = asyncio.run(core.run("解释一下 tool_call 标记"))
    assert "常见的工具调用标记" in result.final_text
    assert not any(e.type == EventType.ERROR for e in result.events)
