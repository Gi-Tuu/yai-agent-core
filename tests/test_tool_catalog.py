"""两层工具目录懒加载测试，全部离线、不需要 API Key。

- 系统提示只放 name + 一句话摘要（轻量目录），完整参数结构在 function-calling 里；
- 工具数在预算内：全量注入完整 schema，零额外模型调用；
- 工具数超预算：先让模型看目录选工具，只注入所选完整 schema；
- 聚焦失败 / 一个都没选中：回退全量 schema，宁多勿漏。
"""

import asyncio

from yai_core import AgentCore, ModelResponse, ToolCallRequest, ToolSpec
from yai_core.tools.registry import ToolRegistry


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
    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self.calls: list[dict] = []

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls.append({"messages": messages, "tools": tools})
        i = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[i]


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

def test_within_budget_injects_all_schemas_without_focus_call() -> None:
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
    _register(core, 3)
    asyncio.run(core.run("查询工具零零"))

    # 第一次模型调用就是 react 本身（tools 非空），没有额外的聚焦调用
    first = model.calls[0]
    assert first["tools"] is not None
    assert len(first["tools"]) == 3


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
