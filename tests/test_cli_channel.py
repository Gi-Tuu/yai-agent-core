"""CliChannel 测试：事件流人可读打印、澄清输入、工具确认。

ask/confirm 在线程里调用内置 input，用 monkeypatch 替换 builtins.input 离线驱动，
不依赖真实终端。
"""

import asyncio

from yai_core.channels.cli import CliChannel
from yai_core.types import AgentEvent, EventType


def _run(coro):
    return asyncio.run(coro)


def test_emit_tool_call(capsys) -> None:
    ev = AgentEvent(
        EventType.TOOL_CALL, {"tool": "search_notes", "arguments": {"keyword": "天气"}}
    )
    _run(CliChannel().emit(ev))
    out = capsys.readouterr().out
    assert "[调用工具]" in out and "search_notes" in out and "天气" in out


def test_emit_strategy_selected(capsys) -> None:
    ev = AgentEvent(EventType.STRATEGY_SELECTED, {"strategy": "react"})
    _run(CliChannel().emit(ev))
    assert "[策略] react" in capsys.readouterr().out


def test_emit_model_message(capsys) -> None:
    ev = AgentEvent(EventType.MODEL_MESSAGE, {"text": "你好呀"})
    _run(CliChannel().emit(ev))
    assert "[回复] 你好呀" in capsys.readouterr().out


def test_emit_known_label_else_branch(capsys) -> None:
    # 非 TOOL_CALL/STRATEGY/MODEL 的已知标签事件走 else，打印整个 data。
    ev = AgentEvent(EventType.PLAN_CREATED, {"steps": ["a", "b"]})
    _run(CliChannel().emit(ev))
    out = capsys.readouterr().out
    assert "[计划]" in out and "a" in out


def test_emit_unknown_type_uses_event_value(capsys) -> None:
    # 不在 _LABELS 的事件类型：label 回退为 event.type.value。
    ev = AgentEvent(EventType.DONE, {"final_text": "结束"})
    _run(CliChannel().emit(ev))
    out = capsys.readouterr().out
    assert "done" in out and "结束" in out


def test_ask_reads_input(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: "我要查天气")
    assert _run(CliChannel().ask("想做什么？")) == "我要查天气"


def test_confirm_yes_variants(monkeypatch) -> None:
    for yes in ("y", "yes", "是"):
        monkeypatch.setattr("builtins.input", lambda prompt, _v=yes: _v)
        assert _run(CliChannel().confirm("search_notes", {})) is True


def test_confirm_default_no(monkeypatch) -> None:
    for no in ("", "n", "no", "随便"):
        monkeypatch.setattr("builtins.input", lambda prompt, _v=no: _v)
        assert _run(CliChannel().confirm("x", {})) is False
