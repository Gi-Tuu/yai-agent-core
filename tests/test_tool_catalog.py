"""两层工具目录懒加载测试，全部离线、不需要 API Key。

- 系统提示只放 name + 一句话摘要（轻量目录），完整参数结构在 function-calling 里；
- 工具数在预算内：全量注入完整 schema，零额外模型调用；
- 工具数超预算：先让模型看目录选工具，只注入所选完整 schema；
- 聚焦失败 / 一个都没选中：回退全量 schema，宁多勿漏。
"""

import asyncio

import pytest

from yai_core import AgentCore, Budget, ModelResponse, ToolCallRequest, ToolSpec
from yai_core.learning import extract_route_outcome
from yai_core.tools.registry import ToolRegistry
from yai_core.types import EventType, TokenUsage


def _stub_handler(**_kwargs: object) -> dict:
    return {"ok": True}


def _spec(index: int, *, long_description: str | None = None) -> ToolSpec:
    return ToolSpec(
        name=f"tool_{index:02d}",
        description=long_description
        or f"第 {index} 号工具的一句话摘要。",
        input_schema={
            "type": "object",
            "properties": {"marker": {"type": "string"}},
        },
        handler=_stub_handler,
        source="native",
    )


def _register(core: AgentCore, n: int) -> None:
    core.register_tools([_spec(i) for i in range(n)])


# ---------- ToolRegistry 轻量目录 / 反注册 ----------

def test_catalog_text_is_one_line_per_tool() -> None:
    registry = ToolRegistry()
    registry.register(
        _spec(1, long_description="首行一句话摘要。\n第二行是不该进目录的长说明 SECRET")
    )
    text = registry.catalog_text()
    assert "tool_01" in text
    assert "首行一句话摘要" in text
    assert "SECRET" not in text  # 只有首行摘要进入轻量目录


def test_unregister_removes_tool_and_reports_presence() -> None:
    registry = ToolRegistry()
    registry.register(_spec(1))
    assert registry.unregister("tool_01") is True
    assert registry.has("tool_01") is False
    assert registry.unregister("tool_01") is False  # 再删返回 False


# ---------- 系统提示使用轻量目录 ----------

class _RecordingModel:
    def __init__(self, responses: list[ModelResponse | BaseException]) -> None:
        self._responses = responses
        self.calls: list[dict] = []

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls.append({"messages": messages, "tools": tools, "tier": tier})
        i = min(len(self.calls) - 1, len(self._responses) - 1)
        response = self._responses[i]
        if isinstance(response, BaseException):
            raise response
        return response


def test_system_prompt_uses_lightweight_catalog() -> None:
    model = _RecordingModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="tool_00", arguments={})],
            ),
            ModelResponse(content="完成"),
        ]
    )
    core = AgentCore(model)
    core.register_tools(
        [_spec(0, long_description="工具零的一句话摘要。\n超长参数细节 LONGDETAIL")]
    )
    asyncio.run(core.run("查询工具零"))

    system_content = next(
        m["content"] for m in model.calls[0]["messages"] if m["role"] == "system"
    )
    assert "工具零的一句话摘要" in system_content
    assert "LONGDETAIL" not in system_content  # 完整描述不进系统提示


# ---------- 预算内：全量 schema，不额外调用模型 ----------

@pytest.mark.parametrize("tool_count", [3, 24])
def test_within_budget_injects_all_schemas_without_focus_call(tool_count: int) -> None:
    model = _RecordingModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="tool_00", arguments={})],
            ),
            ModelResponse(content="完成"),
        ]
    )
    core = AgentCore(model)  # 默认预算 24
    _register(core, tool_count)
    result = asyncio.run(core.run("查询工具零零"))

    # 第一次模型调用就是 react 本身（tools 非空），没有额外的聚焦调用
    first = model.calls[0]
    assert first["tools"] is not None
    assert len(first["tools"]) == tool_count
    assert len(model.calls) == result.events[-1].data["model_calls"] == 2
    messages = [event for event in result.events if event.type == EventType.MODEL_MESSAGE]
    assert len(messages) == 1
    assert messages[0].data == {"text": "完成", "tier": "standard"}
    assert not any(event.data.get("kind") == "schema_focus" for event in result.events)


# ---------- 超预算：聚焦选择，只注入所选 schema ----------

class _FocusModel:
    """第 1 次（聚焦，tools=None）返回工具名数组，第 2 次调用选中工具，第 3 次收尾。"""

    def __init__(self, chosen: str) -> None:
        self.chosen = chosen
        self.calls: list[dict] = []

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls.append({"messages": messages, "tools": tools})
        focus = tools is None and any(
            "工具名称" in (m.get("content") or "") for m in messages
        )
        if focus:
            return ModelResponse(content=f'["{self.chosen}"]')
        if len(self.calls) == 2:
            return ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name=self.chosen, arguments={})],
            )
        return ModelResponse(content="已用选中工具完成")


def test_over_budget_focuses_and_injects_only_chosen_schema() -> None:
    model = _FocusModel("tool_03")
    core = AgentCore(model, full_schema_budget=5)
    _register(core, 30)
    result = asyncio.run(core.run("查询工具零三"))

    # 第一次是聚焦调用（不带 tools），第二次只带选中的 1 个 schema
    assert model.calls[0]["tools"] is None
    focused = model.calls[1]["tools"]
    assert len(focused) == 1
    assert focused[0]["function"]["name"] == "tool_03"
    # 选中工具真正被执行
    called = [e.data["tool"] for e in result.events if e.type.value == "tool_call"]
    assert "tool_03" in called


def test_focus_empty_selection_falls_back_to_all_schemas() -> None:
    class _EmptyFocus(_FocusModel):
        async def achat(self, messages, tools=None, *, tier="standard"):
            self.calls.append({"messages": messages, "tools": tools})
            if tools is None and any(
                "工具名称" in (m.get("content") or "") for m in messages
            ):
                return ModelResponse(content="[]")  # 一个都没选 -> 回退全量
            if len(self.calls) == 2:
                return ModelResponse(
                    content="",
                    tool_calls=[ToolCallRequest(
                        id="c1", name="tool_00", arguments={}
                    )],
                )
            return ModelResponse(content="完成")

    model = _EmptyFocus("tool_00")
    core = AgentCore(model, full_schema_budget=5)
    _register(core, 30)
    asyncio.run(core.run("查询工具零零"))

    # 回退后 react 拿到全部 30 个 schema
    assert len(model.calls[1]["tools"]) == 30


def test_focus_invalid_output_falls_back_to_all_schemas() -> None:
    class _BadFocus(_FocusModel):
        async def achat(self, messages, tools=None, *, tier="standard"):
            self.calls.append({"messages": messages, "tools": tools})
            if tools is None and any(
                "工具名称" in (m.get("content") or "") for m in messages
            ):
                return ModelResponse(content="我没法输出 JSON")  # 非法 -> 回退
            if len(self.calls) == 2:
                return ModelResponse(
                    content="",
                    tool_calls=[ToolCallRequest(
                        id="c1", name="tool_00", arguments={}
                    )],
                )
            return ModelResponse(content="完成")

    model = _BadFocus("tool_00")
    core = AgentCore(model, full_schema_budget=5)
    _register(core, 30)
    asyncio.run(core.run("查询工具零零"))

    assert len(model.calls[1]["tools"]) == 30


@pytest.mark.parametrize(
    ("response", "mode", "reason", "selected"),
    [
        (ModelResponse(content='["tool_01", "ghost", "tool_01"]'),
         "focused", "selected", ["tool_01"]),
        (ModelResponse(content="[]"), "fallback", "empty_selection", ["tool_00", "tool_01"]),
        (ModelResponse(content='["ghost"]'),
         "fallback", "empty_selection", ["tool_00", "tool_01"]),
        (ModelResponse(content="[invalid]"),
         "fallback", "empty_selection", ["tool_00", "tool_01"]),
        (RuntimeError("private provider failure"),
         "fallback", "error", ["tool_00", "tool_01"]),
        (TimeoutError("private timeout details"),
         "fallback", "error", ["tool_00", "tool_01"]),
    ],
)
def test_focus_decision_event_matches_injected_schemas(
    response: ModelResponse | BaseException, mode: str, reason: str, selected: list[str]
) -> None:
    model = _RecordingModel([response, ModelResponse(content="最终答案")])
    core = AgentCore(model, full_schema_budget=1)
    _register(core, 2)
    result = asyncio.run(core.run("查询工具零零"))

    focus_events = [event for event in result.events if event.data.get("kind") == "schema_focus"]
    assert len(focus_events) == 1
    event = focus_events[0]
    assert event.type == EventType.MODEL_MESSAGE
    assert event.data["text"].startswith("工具范围选择：")
    assert event.data["schema_focus"] == {
        "mode": mode,
        "reason": reason,
        "selected": selected,
        "available_count": 2,
    }
    assert "private" not in str(event.data)
    assert "ghost" not in str(event.data)
    assert [schema["function"]["name"] for schema in model.calls[1]["tools"]] == selected
    assert model.calls[0]["tools"] is None
    assert len(model.calls) == result.events[-1].data["model_calls"] == 2
    assert result.events[-1].data["standard_calls"] == 2
    assert result.events[-1].data["strong_calls"] == 0
    assert result.events[0].data["strategy"] == "react"
    assert result.events.index(event) < next(
        i for i, item in enumerate(result.events) if item.type == EventType.TIER_CHANGED
    )
    assert core.channel.events == result.events  # 唯一出口，无重复 emit
    assert len(EventType) == 21

    # 决策文本/选择响应均不加入上下文；记忆与 DONE 只保留真正答案。
    assert [message["role"] for message in model.calls[1]["messages"]] == ["system", "user"]
    assert model.calls[1]["messages"][-1]["content"] == "查询工具零零"
    assert result.final_text == result.events[-1].data["final_text"] == "最终答案"
    assert [message.content for message in core.memory.history()] == ["查询工具零零", "最终答案"]
    without_focus = [item for item in result.events if item is not event]
    assert extract_route_outcome(result.events, result.strategy) == extract_route_outcome(
        without_focus, result.strategy
    )


@pytest.mark.parametrize("failure", ["model_budget", "token_budget", "cancel"])
def test_focus_budget_and_cancellation_do_not_fall_back(failure: str) -> None:
    model = _RecordingModel([
        asyncio.CancelledError() if failure == "cancel" else ModelResponse(
            content='["tool_00"]', usage=TokenUsage(total_tokens=1)
        ),
    ])
    budget = (
        Budget(max_model_calls=0) if failure == "model_budget"
        else Budget(max_tokens=1) if failure == "token_budget"
        else None
    )
    core = AgentCore(model, full_schema_budget=1, budget=budget)
    _register(core, 2)
    events = []

    async def collect() -> None:
        async for event in core.astream("查询工具零零"):
            events.append(event)

    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(collect())
        assert not any(event.type == EventType.DONE for event in events)
    else:
        asyncio.run(collect())
        assert any(event.type == EventType.BUDGET_EXCEEDED for event in events)
        assert events[-1].data["reason"] == "budget_exceeded"
    assert len(model.calls) == (0 if failure == "model_budget" else 1)
    assert not any(event.data.get("kind") == "schema_focus" for event in events)
    assert not any(event.type == EventType.ERROR for event in events)


@pytest.mark.parametrize("answer", [ModelResponse(content=""), RuntimeError("react failed")])
def test_focus_message_never_becomes_done_or_history_answer(
    answer: ModelResponse | BaseException,
) -> None:
    model = _RecordingModel([ModelResponse(content='["tool_00"]'), answer])
    core = AgentCore(model, full_schema_budget=1)
    _register(core, 2)
    result = asyncio.run(core.run("查询工具零零"))

    assert result.final_text == result.events[-1].data["final_text"] == ""
    assert core.memory.history()[-1].content == ""
    assert len(model.calls) == 2
    outcome = extract_route_outcome(result.events, result.strategy)
    without_focus = [event for event in result.events if event.data.get("kind") != "schema_focus"]
    baseline = extract_route_outcome(without_focus, result.strategy)
    assert outcome == baseline
    assert outcome.final_empty and not outcome.success
    assert outcome.errors == int(isinstance(answer, BaseException))
