"""ToolDiscovery 最小闭环测试：静态目录匹配 + 缺口→发现→注册→使用。

全部离线：脚本化假模型 + 进程内静态目录，不依赖网络与 API Key。
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

# ---------- 测试用能力 ----------

def search_notes(keyword: str) -> list:
    """搜索宿主笔记库。"""
    return []


def get_weather(city: str) -> dict:
    """查询指定城市的实时天气。"""
    return {"city": city, "weather": "晴", "temp_c": 27}


def _catalog_with_weather() -> StaticCatalog:
    return StaticCatalog(
        [DiscoveredCandidate(get_weather, ("天气", "气温", "weather"))]
    )


# ---------- StaticCatalog 单元测试 ----------

def test_catalog_matches_keyword_in_need() -> None:
    catalog = _catalog_with_weather()
    specs = asyncio.run(
        catalog.discover("天气查询能力", task="别的内容", available=["search_notes"])
    )
    assert [s.name for s in specs] == ["get_weather"]
    assert specs[0].source == "native"


def test_catalog_matches_keyword_in_task() -> None:
    catalog = _catalog_with_weather()
    specs = asyncio.run(
        catalog.discover("某外部能力", task="帮我查一下湛江天气", available=[])
    )
    assert [s.name for s in specs] == ["get_weather"]


def test_catalog_english_keyword_case_insensitive() -> None:
    catalog = _catalog_with_weather()
    specs = asyncio.run(
        catalog.discover("weather lookup", task="", available=[])
    )
    assert len(specs) == 1


def test_catalog_skips_already_available() -> None:
    catalog = _catalog_with_weather()
    specs = asyncio.run(
        catalog.discover("天气", task="", available=["get_weather"])
    )
    assert specs == []


def test_catalog_no_match_returns_empty() -> None:
    catalog = _catalog_with_weather()
    specs = asyncio.run(
        catalog.discover("股票行情能力", task="查一下股价", available=[])
    )
    assert specs == []


def test_catalog_candidate_without_keywords_never_matches() -> None:
    catalog = StaticCatalog(
        [DiscoveredCandidate(get_weather, ())]
    )
    specs = asyncio.run(
        catalog.discover("天气查询能力", task="查天气", available=[])
    )
    assert specs == []


def test_catalog_dedupes_same_name_candidates() -> None:
    catalog = StaticCatalog(
        [
            DiscoveredCandidate(get_weather, ("天气",)),
            DiscoveredCandidate(get_weather, ("气温",)),
        ]
    )
    specs = asyncio.run(
        catalog.discover("天气", task="", available=[])
    )
    assert [s.name for s in specs] == ["get_weather"]


def test_catalog_description_override() -> None:
    catalog = StaticCatalog(
        [DiscoveredCandidate(get_weather, ("天气",), description="自定义天气描述")]
    )
    specs = asyncio.run(
        catalog.discover("天气", task="", available=[])
    )
    assert specs[0].description == "自定义天气描述"


# ---------- 闭环：缺口 → 发现 → 注册 → 本轮使用 ----------

class _GapThenCallModel:
    """第 1 次分类（报缺口），第 2 次调用新工具，第 3 次收尾。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content='{"strategy":"react","tier":"standard","reason":"缺天气",'
                '"missing_capability":"天气查询能力"}'
            )
        if self.calls == 2:
            return ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="w1", name="get_weather", arguments={"city": "湛江"}
                )],
            )
        return ModelResponse(content="湛江当前晴，27℃。")


def test_gap_discovers_registers_and_uses_tool_in_same_run() -> None:
    model = _GapThenCallModel()
    core = AgentCore(
        model, llm_router=True, discovery=_catalog_with_weather()
    )
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下湛江天气"))

    types = [e.type.value for e in result.events]
    assert "capability_missing" in types
    discovered = next(e for e in result.events if e.type.value == "tool_discovered")
    assert discovered.data["registered"] == ["get_weather"]
    assert discovered.data["source"] == "StaticCatalog"
    # 新工具在本轮 react 循环里真正被调用
    call = next(e for e in result.events if e.type.value == "tool_call")
    assert call.data["tool"] == "get_weather"
    result_ev = next(e for e in result.events if e.type.value == "tool_result")
    assert result_ev.data["ok"] is True
    assert "27" in result.final_text
    assert core.list_tools()  # 注册后长期可用
    assert "get_weather" in [t["name"] for t in core.list_tools()]


def test_no_discovery_source_keeps_legacy_behavior() -> None:
    # 安全默认：不配置发现源时，只发缺口事件，不动态注册任何工具。
    model = _GapThenCallModel()
    core = AgentCore(model, llm_router=True)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下湛江天气"))

    types = [e.type.value for e in result.events]
    assert "capability_missing" in types
    assert "tool_discovered" not in types
    assert "get_weather" not in [t["name"] for t in core.list_tools()]


class _StockGapModel(_GapThenCallModel):
    """分类器报"股票"缺口（与天气目录不匹配），之后直接收尾。"""

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content='{"strategy":"react","reason":"缺股票",'
                '"missing_capability":"股票行情能力"}'
            )
        return ModelResponse(content="我当前没有股票行情能力。")


def test_catalog_no_match_emits_no_discovery_event() -> None:
    model = _StockGapModel()
    catalog = StaticCatalog([DiscoveredCandidate(get_weather, ("天气",))])
    core = AgentCore(model, llm_router=True, discovery=catalog)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下某股票价格"))
    types = [e.type.value for e in result.events]
    assert "capability_missing" in types
    assert "tool_discovered" not in types


class _BoomDiscovery:
    async def discover(self, need, *, task, available):
        raise RuntimeError("目录服务不可用")


def test_discovery_failure_is_non_fatal() -> None:
    model = _GapThenCallModel()
    core = AgentCore(model, llm_router=True, discovery=_BoomDiscovery())
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下湛江天气"))

    err = next(
        e for e in result.events
        if e.type.value == "error" and e.data.get("stage") == "tool_discovery"
    )
    assert "目录服务不可用" in err.data["error"]
    assert result.events[-1].type.value == "done"  # 未中断，照常收尾


def test_discovered_tool_still_gated_by_permission_policy() -> None:
    """发现不等于授权：auto 策略下新工具不在白名单，执行时仍需确认。"""

    class _DenyChannel(CollectChannel):
        def __init__(self) -> None:
            super().__init__(auto_confirm=False)
            self.asked: list[str] = []

        async def confirm(self, tool_name, arguments):
            self.asked.append(tool_name)
            return False

    channel = _DenyChannel()
    model = _GapThenCallModel()
    core = AgentCore(
        model,
        llm_router=True,
        discovery=_catalog_with_weather(),
        auto_approve_tools=False,
        channel=channel,
    )
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下湛江天气"))

    # 工具被发现并注册
    assert any(e.type.value == "tool_discovered" for e in result.events)
    # 但执行被权限策略拦下：有 permission_asked，没有真正的 tool_call。
    asked = [e for e in result.events if e.type.value == "permission_asked"]
    assert any(e.data["tool"] == "get_weather" for e in asked)
    called = [e for e in result.events if e.type.value == "tool_call"]
    assert all(e.data["tool"] != "get_weather" for e in called)
    assert "get_weather" in channel.asked
