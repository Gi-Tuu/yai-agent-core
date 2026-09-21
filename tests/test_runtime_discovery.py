"""执行中动态发现测试：react 循环里模型可调用 request_capability 临时长出工具。

与"路由阶段一次性发现"（test_discovery_catalog.py）不同，这里验证模型在
执行到一半、意识到缺工具时，能通过 meta-tool 显式请求发现，注册后下一轮
立即调用新工具。全部离线，不需要 API Key。
"""

import asyncio

from yai_core import (
    AgentCore,
    DiscoveredCandidate,
    ModelResponse,
    StaticCatalog,
    ToolCallRequest,
    build_spec,
)
from yai_core.channels.collect import CollectChannel


def search_notes(keyword: str) -> list:
    """搜索宿主笔记库。"""
    return []


def get_weather(city: str) -> dict:
    """查询指定城市的实时天气。"""
    return {"city": city, "weather": "晴", "temp_c": 27}


def _weather_catalog() -> StaticCatalog:
    return StaticCatalog([DiscoveredCandidate(get_weather, ("天气", "气温", "weather"))])


class _MidRunGapModel:
    """第 1 轮请求发现天气能力，第 2 轮调用新工具，第 3 轮收尾。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="r1",
                        name="request_capability",
                        arguments={"need": "天气查询能力"},
                    )
                ],
            )
        if self.calls == 2:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="w1", name="get_weather", arguments={"city": "湛江"}
                    )
                ],
            )
        return ModelResponse(content="湛江当前晴，27℃。")


def test_request_capability_registered_only_with_discovery() -> None:
    core = AgentCore(_MidRunGapModel())
    core.register_tools([build_spec(search_notes)])
    names = [t["name"] for t in core.list_tools()]
    assert "request_capability" not in names  # 无发现源：不注册空承诺工具

    core2 = AgentCore(_MidRunGapModel(), discovery=_weather_catalog())
    core2.register_tools([build_spec(search_notes)])
    assert "request_capability" in [t["name"] for t in core2.list_tools()]


def test_mid_run_gap_discovers_and_uses_new_tool() -> None:
    model = _MidRunGapModel()
    core = AgentCore(model, discovery=_weather_catalog())
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下湛江天气"))
    types = [e.type.value for e in result.events]

    # 执行中缺口事件带 phase=react，区别于路由阶段
    gap = next(e for e in result.events if e.type.value == "capability_missing")
    assert gap.data.get("phase") == "react"
    assert gap.data["missing"] == "天气查询能力"
    # 发现并注册
    discovered = next(e for e in result.events if e.type.value == "tool_discovered")
    assert discovered.data["registered"] == ["get_weather"]
    # 新工具在后续轮次真正被调用
    assert "tool_call" in types
    calls = [e.data["tool"] for e in result.events if e.type.value == "tool_call"]
    assert calls == ["request_capability", "get_weather"]
    assert "27" in result.final_text
    assert "get_weather" in [t["name"] for t in core.list_tools()]


class _DenyChannel(CollectChannel):
    def __init__(self) -> None:
        super().__init__(auto_confirm=False)
        self.asked: list[str] = []

    async def confirm(self, tool_name, arguments):
        self.asked.append(tool_name)
        return False


def test_mid_run_discovery_gated_by_permission() -> None:
    channel = _DenyChannel()
    model = _MidRunGapModel()
    core = AgentCore(
        model,
        discovery=_weather_catalog(),
        auto_approve_tools=False,
        channel=channel,
    )
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下湛江天气"))

    asked = [e for e in result.events if e.type.value == "permission_asked"]
    assert any(e.data["tool"] == "request_capability" for e in asked)
    assert "request_capability" in channel.asked
    # 被拒绝后不发现、不注册
    assert not [e for e in result.events if e.type.value == "tool_discovered"]
    assert "get_weather" not in [t["name"] for t in core.list_tools()]


class _NoMatchModel:
    """请求发现一个目录里没有的能力，然后如实说明。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="r1",
                        name="request_capability",
                        arguments={"need": "股票行情能力"},
                    )
                ],
            )
        return ModelResponse(content="我当前没有股票行情能力，无法查询。")


def test_mid_run_discovery_no_match_reports_gap() -> None:
    model = _NoMatchModel()
    core = AgentCore(model, discovery=_weather_catalog())
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下某股票价格"))
    types = [e.type.value for e in result.events]
    assert "capability_missing" in types
    assert "tool_discovered" not in types
    assert "股票" in result.final_text
    assert result.events[-1].type.value == "done"


class _MissingNeedModel:
    """模型调用 request_capability 时漏传 need，内核应回灌错误而不是崩溃。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="r1", name="request_capability", arguments={}
                    )
                ],
            )
        return ModelResponse(content="我需要先明确缺什么能力。")


def test_request_capability_without_need_is_non_fatal() -> None:
    model = _MissingNeedModel()
    core = AgentCore(model, discovery=_weather_catalog())
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("随便做点什么"))
    assert result.events[-1].type.value == "done"
    assert not [e for e in result.events if e.type.value == "tool_discovered"]
