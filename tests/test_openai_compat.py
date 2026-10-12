"""OpenAICompatProvider 协议层测试：用假 openai 模块离线验证 DeepSeek 同款链路。

不发任何网络请求，验证四件事：
1. 第一次请求携带 OpenAI function-calling 的 tools 描述；
2. provider 能把 SDK 返回的 tool_calls 解析成 ToolCallRequest；
3. 宿主函数被真正执行；
4. 第二次请求把结果以 role="tool" + tool_call_id 回灌（DeepSeek/OpenAI 硬性协议）。
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from yai_core import AgentCore, OpenAICompatProvider, build_spec

# ---------- 假 openai SDK ----------

class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.type = "function"
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content: str | None, tool_calls: list[_FakeToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage) -> None:
        self.message = message


class _FakeCompletions:
    def __init__(self, script: list[_FakeMessage], captured: list[dict[str, Any]]) -> None:
        self._script = script
        self._captured = captured
        self.calls = 0

    async def create(self, **kwargs: Any) -> Any:
        self._captured.append(kwargs)
        message = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return types.SimpleNamespace(choices=[_FakeChoice(message)])


class _FakeAsyncOpenAI:
    script: list[_FakeMessage] = []
    captured: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.chat = types.SimpleNamespace(
            completions=_FakeCompletions(_FakeAsyncOpenAI.script, _FakeAsyncOpenAI.captured)
        )


def _install_fake_openai(
    monkeypatch, script: list[_FakeMessage], captured: list[dict[str, Any]]
) -> None:
    _FakeAsyncOpenAI.script = script
    _FakeAsyncOpenAI.captured = captured
    fake_module = types.ModuleType("openai")
    fake_module.AsyncOpenAI = _FakeAsyncOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_module)


# ---------- 宿主能力（普通函数） ----------

def count_notes() -> int:
    """统计笔记总数。"""
    return 3


def test_tool_call_roundtrip_and_tool_message_feedback(monkeypatch) -> None:
    script = [
        # 第一轮：模型决定调 count_notes
        _FakeMessage(None, [_FakeToolCall("call_1", "count_notes", "{}")]),
        # 第二轮：模型拿到工具结果后收尾
        _FakeMessage("你一共有 3 条笔记。", None),
    ]
    captured: list[dict[str, Any]] = []
    _install_fake_openai(monkeypatch, script, captured)

    provider = OpenAICompatProvider(api_key="test-key", base_url="http://localhost:1234")
    core = AgentCore(provider)
    core.register_tools([build_spec(count_notes)])

    result = asyncio.run(core.run("统计一下笔记数量"))

    event_types = [e.type.value for e in result.events]
    assert result.strategy.value == "react"
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "3 条" in result.final_text

    # 第一次请求必须带 tools 清单，且形状是 OpenAI function-calling 规范
    first_request = captured[0]
    assert first_request["tools"][0]["type"] == "function"
    assert first_request["tools"][0]["function"]["name"] == "count_notes"

    # 第二次请求必须把工具结果以 role=tool 回灌，id 与模型下发的一致
    second_messages = captured[1]["messages"]
    tool_messages = [m for m in second_messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert tool_messages[0]["content"] == "3"


def test_plain_answer_sends_no_tools(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    _install_fake_openai(monkeypatch, [_FakeMessage("你好，我是内嵌助手。", None)], captured)

    provider = OpenAICompatProvider(api_key="test-key", base_url="http://localhost:1234")
    core = AgentCore(provider)
    result = asyncio.run(core.run("你好"))

    assert result.strategy.value == "direct"
    assert result.final_text.startswith("你好")
    # direct 策略不应下发 tools
    assert "tools" not in captured[0]


# ---------- 协议层边界补测：不经过 AgentCore，直接驱动 provider.achat ----------


class _FakeUsage:
    def __init__(self, prompt: int, completion: int, total: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class _OneShotCompletions:
    """记录入参并只返回一个固定 completion 的假 completions。"""

    def __init__(self, completion: Any) -> None:
        self._completion = completion
        self.kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self._completion


def _provider_with(
    monkeypatch, *, message: _FakeMessage, usage: _FakeUsage | None = None, **provider_kwargs: Any
) -> tuple[OpenAICompatProvider, _OneShotCompletions]:
    """装假 openai、构造 provider，再塞入一次性假 client，返回 (provider, 入参记录器)。"""
    _install_fake_openai(monkeypatch, [message], [])
    provider = OpenAICompatProvider(
        api_key="test-key", base_url="http://localhost:1234", **provider_kwargs
    )
    completion = types.SimpleNamespace(choices=[_FakeChoice(message)], usage=usage)
    completions = _OneShotCompletions(completion)
    provider.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    return provider, completions


def test_missing_openai_extra_raises_friendly_error(monkeypatch) -> None:
    # sys.modules 里 openai=None 会让 `from openai import AsyncOpenAI` 抛 ImportError，
    # provider 必须把它翻译成带安装指引的友好错误，而不是裸 ImportError。
    monkeypatch.setitem(sys.modules, "openai", None)
    with pytest.raises(ImportError, match="可选依赖"):
        OpenAICompatProvider(api_key="test-key")


class _CapturingAsyncOpenAI:
    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs


def _install_capturing_openai(monkeypatch) -> None:
    fake_module = types.ModuleType("openai")
    fake_module.AsyncOpenAI = _CapturingAsyncOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_module)


def test_provider_forwards_retry_and_timeout(monkeypatch) -> None:
    _install_capturing_openai(monkeypatch)
    OpenAICompatProvider(api_key="k", max_retries=0, timeout=5.0)
    assert _CapturingAsyncOpenAI.last_kwargs["max_retries"] == 0
    assert _CapturingAsyncOpenAI.last_kwargs["timeout"] == 5.0


def test_provider_omits_optional_client_kwargs_by_default(monkeypatch) -> None:
    _install_capturing_openai(monkeypatch)
    OpenAICompatProvider(api_key="k")
    assert "max_retries" not in _CapturingAsyncOpenAI.last_kwargs
    assert "timeout" not in _CapturingAsyncOpenAI.last_kwargs


def test_malformed_tool_arguments_fallback_to_empty_dict(monkeypatch) -> None:
    msg = _FakeMessage(None, [_FakeToolCall("c1", "do_it", "{不是合法 json")])
    provider, _ = _provider_with(monkeypatch, message=msg)
    resp = asyncio.run(
        provider.achat([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
    )
    assert resp.tool_calls[0].name == "do_it"
    # 模型给出非法 JSON 时容错为空 dict，绝不让一次坏参数炸掉整轮。
    assert resp.tool_calls[0].arguments == {}


def test_usage_parsed_and_total_falls_back_to_sum(monkeypatch) -> None:
    msg = _FakeMessage("完成", None)
    # total_tokens=0：内核用 prompt+completion 兜底，不臆造但也不漏算。
    provider, completions = _provider_with(
        monkeypatch, message=msg, usage=_FakeUsage(10, 5, 0)
    )
    resp = asyncio.run(provider.achat([{"role": "user", "content": "x"}]))
    assert resp.usage is not None
    assert resp.usage.prompt_tokens == 10
    assert resp.usage.completion_tokens == 5
    assert resp.usage.total_tokens == 15
    # 不传 tools 时不下发 tool_choice。
    assert "tools" not in (completions.kwargs or {})


def test_usage_prefers_reported_total(monkeypatch) -> None:
    msg = _FakeMessage("完成", None)
    provider, _ = _provider_with(monkeypatch, message=msg, usage=_FakeUsage(10, 5, 20))
    resp = asyncio.run(provider.achat([{"role": "user", "content": "x"}]))
    assert resp.usage is not None
    assert resp.usage.total_tokens == 20


def test_strong_tier_selects_strong_model(monkeypatch) -> None:
    msg = _FakeMessage("强模型答复", None)
    provider, completions = _provider_with(
        monkeypatch, message=msg, model="std-model", strong_model="strong-model"
    )
    asyncio.run(provider.achat([{"role": "user", "content": "x"}], tier="strong"))
    assert (completions.kwargs or {})["model"] == "strong-model"
    asyncio.run(provider.achat([{"role": "user", "content": "x"}], tier="standard"))
    assert (completions.kwargs or {})["model"] == "std-model"
