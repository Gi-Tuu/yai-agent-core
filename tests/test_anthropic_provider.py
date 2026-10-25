"""AnthropicProvider 协议层测试：假 anthropic 模块离线验证 Messages API 翻译。

不发任何网络请求。钉的是"OpenAI 线格式 → Anthropic 形状"最容易写错的四处：
system 走顶层参数、max_tokens 必填、tool_result 必须挂在 user 消息上、
一次调用里的多个工具结果要合并进同一条 user 消息。
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from yai_core import AgentCore, AnthropicProvider, build_spec
from yai_core.llm.anthropic import (
    parse_response,
    split_system,
    to_anthropic_messages,
    to_anthropic_tools,
)

# ---------- 纯翻译函数 ----------


def test_split_system_merges_and_strips() -> None:
    system, rest = split_system(
        [
            {"role": "system", "content": " 你是助手 "},
            {"role": "system", "content": "只用中文"},
            {"role": "user", "content": "数一下"},
        ]
    )
    assert system == "你是助手\n\n只用中文"
    assert [m["role"] for m in rest] == ["user"]


def test_split_system_absent_returns_none() -> None:
    system, rest = split_system([{"role": "user", "content": "hi"}])
    assert system is None
    assert len(rest) == 1


def test_assistant_tool_calls_become_tool_use_blocks() -> None:
    converted = to_anthropic_messages(
        [
            {
                "role": "assistant",
                "content": "我先查一下",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "count_notes", "arguments": "{}"}},
                    {"id": "c2", "function": {"name": "bad", "arguments": "{不是 JSON"}},
                ],
            }
        ]
    )
    assert converted[0]["role"] == "assistant"
    blocks = converted[0]["content"]
    assert blocks[0] == {"type": "text", "text": "我先查一下"}
    assert blocks[1] == {"type": "tool_use", "id": "c1", "name": "count_notes", "input": {}}
    # 参数解析失败按空对象处理：绝不把坏 JSON 原样塞给上游
    assert blocks[2]["input"] == {}


def test_assistant_without_text_emits_no_empty_text_block() -> None:
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "c1", "function": {"name": "n", "arguments": "{}"}}],
    }
    converted = to_anthropic_messages([message])
    assert [b["type"] for b in converted[0]["content"]] == ["tool_use"]


def test_tool_results_merge_into_one_user_message() -> None:
    converted = to_anthropic_messages(
        [
            {"role": "user", "content": "查两个"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "function": {"name": "a", "arguments": "{}"}},
                    {"id": "c2", "function": {"name": "b", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "a", "content": "3"},
            {"role": "tool", "tool_call_id": "c2", "name": "b", "content": "5"},
        ]
    )
    roles = [m["role"] for m in converted]
    assert roles == ["user", "assistant", "user"]
    results = converted[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result"]
    assert results[0] == {"type": "tool_result", "tool_use_id": "c1", "content": "3"}
    assert results[1]["tool_use_id"] == "c2"


def test_openai_tool_shape_unwraps_to_input_schema() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "count_notes",
                "description": "统计笔记",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {"type": "function", "function": {"name": "no_desc", "parameters": {}}},
        {"type": "function", "function": {"name": "", "parameters": {}}},
    ]
    converted = to_anthropic_tools(tools)
    assert converted[0] == {
        "name": "count_notes",
        "input_schema": {"type": "object", "properties": {}},
        "description": "统计笔记",
    }
    assert "description" not in converted[1]
    assert len(converted) == 2  # 无名工具直接丢掉


def test_missing_parameters_falls_back_to_empty_object() -> None:
    assert to_anthropic_tools([{"function": {"name": "x"}}])[0]["input_schema"] == {
        "type": "object",
        "properties": {}
    }


# ---------- 假 anthropic SDK ----------


class _Block:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class _Usage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.input_tokens = prompt
        self.output_tokens = completion


class _Response:
    def __init__(self, content: list[_Block], usage: _Usage | None = None) -> None:
        self.content = content
        self.usage = usage
        self.stop_reason = "end_turn"


class _FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[_Response] = []

    async def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        # 只关心"发出去的请求长什么样"的用例会连问两次；没有预置响应时给一条
        # 中性文本，让测试自己决定断言请求还是断言回路。
        if self.responses:
            return self.responses.pop(0)
        return _Response([_Block(type="text", text="ok")])


class _FakeClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.messages = _FakeMessages()


def _provider_with(responses: list[_Response], **init: Any) -> AnthropicProvider:
    provider = AnthropicProvider(client=_FakeClient(), **init)
    provider.client.messages.responses = responses  # type: ignore[attr-defined]
    return provider


def test_parse_response_joins_text_and_maps_tool_use() -> None:
    response = _Response(
        [
            _Block(type="text", text="一共"),
            _Block(type="thinking", text="忽略我"),
            _Block(type="tool_use", id="c1", name="count_notes", input={"scope": "all"}),
        ],
        _Usage(120, 30),
    )
    parsed = parse_response(response)
    assert parsed.content == "一共"
    assert parsed.usage is not None
    assert (parsed.usage.prompt_tokens, parsed.usage.completion_tokens) == (120, 30)
    assert parsed.usage.total_tokens == 150
    assert parsed.tool_calls[0].name == "count_notes"
    assert parsed.tool_calls[0].arguments == {"scope": "all"}


def test_parse_response_without_usage_keeps_none() -> None:
    parsed = parse_response(_Response([_Block(type="text", text="好")]))
    assert parsed.usage is None  # 内核不臆造 token 数，预算改用次数兜底


@pytest.mark.parametrize("arguments", [None, [], "", "secret-argument", 1, True])
@pytest.mark.parametrize("dict_block", [False, True])
def test_parse_response_rejects_non_object_arguments(arguments, dict_block) -> None:
    block = {"type": "tool_use", "id": "c1", "name": "get_records", "input": arguments}
    response = types.SimpleNamespace(
        content=[block if dict_block else types.SimpleNamespace(**block)]
    )
    with pytest.raises(ValueError, match="参数必须是合法 JSON 对象") as exc:
        parse_response(response)
    assert "secret-argument" not in str(exc.value)


@pytest.mark.parametrize("valid_first", [False, True])
def test_invalid_response_does_not_execute_defaults(valid_first) -> None:
    calls = []

    def get_records(limit: int = 100) -> list:
        calls.append(limit)
        return []

    blocks = [types.SimpleNamespace(type="tool_use", id="bad", name="get_records", input=None)]
    if valid_first:
        blocks.insert(0, types.SimpleNamespace(
            type="tool_use", id="good", name="get_records", input={"limit": 1}
        ))
    provider = _provider_with([_Response(blocks)])
    core = AgentCore(provider)
    core.register_tools([build_spec(get_records)])
    result = asyncio.run(core.run("查询记录"))
    assert calls == []
    assert any(event.type.value == "error" for event in result.events)
    assert not any(event.type.value == "tool_call" for event in result.events)


def test_achat_puts_system_on_top_level_and_requires_max_tokens() -> None:
    provider = _provider_with([_Response([_Block(type="text", text="ok")])], max_tokens=777)
    asyncio.run(
        provider.achat(
            [
                {"role": "system", "content": "你是笔记助手"},
                {"role": "user", "content": "数一下"},
            ]
        )
    )
    sent = provider.client.messages.calls[0]
    assert sent["system"] == "你是笔记助手"
    assert sent["max_tokens"] == 777
    assert all(message["role"] != "system" for message in sent["messages"])


def test_tier_selects_strong_model() -> None:
    provider = _provider_with(
        [_Response([_Block(type="text", text="ok")])],
        model="small",
        strong_model="big",
    )
    client = provider.client
    asyncio.run(provider.achat([{"role": "user", "content": "x"}], tier="standard"))
    assert client.messages.calls[0]["model"] == "small"
    asyncio.run(provider.achat([{"role": "user", "content": "x"}], tier="strong"))
    assert client.messages.calls[1]["model"] == "big"


def test_tools_only_sent_when_present() -> None:
    provider = _provider_with([_Response([_Block(type="text", text="ok")])])
    asyncio.run(provider.achat([{"role": "user", "content": "x"}]))
    assert "tools" not in provider.client.messages.calls[0]
    asyncio.run(
        provider.achat(
            [{"role": "user", "content": "x"}],
            [{"type": "function", "function": {"name": "a", "parameters": {}}}],
        )
    )
    assert provider.client.messages.calls[1]["tools"][0]["name"] == "a"


def test_env_configuration_is_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "m-from-env")
    monkeypatch.setenv("ANTHROPIC_STRONG_MODEL", "m-strong-env")
    monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "2048")
    provider = AnthropicProvider(client=_FakeClient())
    assert (provider.model, provider.strong_model, provider.max_tokens) == (
        "m-from-env",
        "m-strong-env",
        2048,
    )


def test_strong_model_defaults_to_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_STRONG_MODEL", raising=False)
    provider = AnthropicProvider(client=_FakeClient(), model="only-one")
    assert provider.strong_model == "only-one"


def test_missing_sdk_gives_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(ImportError, match="uv sync --extra anthropic"):
        AnthropicProvider(api_key="sk-test")


def test_provider_declares_live_router_flag() -> None:
    assert AnthropicProvider.yai_live_router is True


# ---------- 端到端：一次真实工具回路 ----------


def count_notes() -> int:
    """统计笔记总数。"""
    return 3


def test_tool_roundtrip_through_agent_core(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("anthropic")
    fake.AsyncAnthropic = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    provider = AnthropicProvider(api_key="sk-test", model="claude-test")
    provider.client.messages.responses = [
        _Response(
            [_Block(type="tool_use", id="c1", name="count_notes", input={})],
            _Usage(50, 10),
        ),
        _Response([_Block(type="text", text="共 3 条笔记")], _Usage(70, 12)),
    ]

    core = AgentCore(provider, llm_router=False)
    core.register_tools([build_spec(count_notes)])
    result = asyncio.run(core.run("统计一下笔记数量"))

    assert result.final_text == "共 3 条笔记"
    second = provider.client.messages.calls[1]
    # Anthropic 硬性协议：工具结果以 user 消息里的 tool_result 块回灌，带原 tool_use_id
    assert second["messages"][-1]["role"] == "user"
    assert second["messages"][-1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "c1",
        "content": "3",
    }
    # 第一次请求就要带上工具描述，且形状是 Anthropic 的 name/input_schema
    assert provider.client.messages.calls[0]["tools"][0]["name"] == "count_notes"
    assert provider.client.messages.calls[0]["tools"][0]["input_schema"]["type"] == "object"


# ---------- 容错分支：网关常把对象回成 dict ----------


def test_as_text_accepts_parts_list_and_none() -> None:
    system, rest = split_system(
        [
            {"role": "system", "content": None},
            {"role": "system", "content": [{"type": "text", "text": "第一段"}, {"type": "image"}]},
            {"role": "user", "content": 42},
        ]
    )
    assert system == "第一段"          # 非文本块没有 text 键 → 贡献空串
    assert to_anthropic_messages(rest)[0]["content"][0]["text"] == "42"


def test_message_with_nothing_to_say_is_dropped() -> None:
    converted = to_anthropic_messages(
        [{"role": "user", "content": ""}, {"role": "user", "content": "在"}]
    )
    assert len(converted) == 1


def test_tool_result_without_id_still_translates() -> None:
    converted = to_anthropic_messages([{"role": "tool", "content": "3"}])
    assert converted[0]["content"][0]["tool_use_id"] == ""


def test_parse_response_accepts_dict_blocks_and_dict_usage() -> None:
    class _DictResponse:
        content = [
            {"type": "text", "text": "好"},
            {"type": "tool_use", "id": "c9", "name": "go", "input": {"a": 1}},
        ]
        usage = {"input_tokens": 11, "output_tokens": 5}

    parsed = parse_response(_DictResponse())
    assert parsed.content == "好"
    assert parsed.tool_calls[0].name == "go"
    assert parsed.usage is not None and parsed.usage.total_tokens == 16


def test_client_kwargs_forward_base_url_retry_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _CatchingClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    fake = types.ModuleType("anthropic")
    fake.AsyncAnthropic = _CatchingClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.invalid/v1")

    AnthropicProvider(api_key="sk-test", max_retries=0, timeout=3.5)
    assert captured == {
        "api_key": "sk-test",
        "base_url": "https://gateway.invalid/v1",
        "max_retries": 0,
        "timeout": 3.5,
    }


def test_tool_arguments_may_arrive_pre_parsed_or_garbage() -> None:
    converted = to_anthropic_messages(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "function": {"name": "a", "arguments": {"n": 1}}},
                    {"id": "c2", "function": {"name": "b", "arguments": ["坏形状"]}},
                ],
            }
        ]
    )
    blocks = converted[0]["content"]
    assert blocks[0]["input"] == {"n": 1}     # 已经是 dict 就直接用，不再 json.loads
    assert blocks[1]["input"] == {}           # 非 dict 一律收成空对象，不转发垃圾
