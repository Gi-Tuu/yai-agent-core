"""v0.8 远程发现源测试：假 bridge 离线验证，零网络、零第三方包。"""

from __future__ import annotations

import asyncio

from yai_core.discovery.remote_sources import (
    RemoteSourceDiscovery,
    mcp_discovery,
    openapi_discovery,
)
from yai_core.types import ToolSpec


def _spec(name: str, source: str = "mcp") -> ToolSpec:
    return ToolSpec(
        name=name, description=name, input_schema={}, handler=None, source=source
    )


class FakeBridge:
    """记录连接次数 / 关闭状态；fail=True 时 connect 抛错模拟源不可用。"""

    def __init__(self, specs: list[ToolSpec], *, fail: bool = False) -> None:
        self._specs = specs
        self.fail = fail
        self.connect_calls = 0
        self.closed = False

    async def connect(self) -> list[ToolSpec]:
        self.connect_calls += 1
        if self.fail:
            raise RuntimeError("source down")
        return list(self._specs)

    async def aclose(self) -> None:
        self.closed = True


def run(coro):
    return asyncio.run(coro)


def test_aggregates_and_dedupes() -> None:
    b1 = FakeBridge([_spec("a"), _spec("b")])
    b2 = FakeBridge([_spec("b"), _spec("c", "openapi")])  # b 跨源重名
    d = RemoteSourceDiscovery([b1, b2])
    out = run(d.discover("need", task="t", available=["c"]))  # c 已注册
    assert [s.name for s in out] == ["a", "b"]


def test_single_source_failure_is_skipped() -> None:
    b1 = FakeBridge([_spec("a")], fail=True)
    b2 = FakeBridge([_spec("b")])
    d = RemoteSourceDiscovery([b1, b2])
    out = run(d.discover("need", task="t", available=[]))
    assert [s.name for s in out] == ["b"]


def test_lazy_connect_happens_once() -> None:
    b1 = FakeBridge([_spec("a")])
    d = RemoteSourceDiscovery([b1])
    run(d.discover("need", task="t", available=[]))
    run(d.discover("need", task="t", available=[]))
    assert b1.connect_calls == 1


def test_aclose_closes_all_bridges() -> None:
    b1 = FakeBridge([_spec("a")])
    b2 = FakeBridge([_spec("b")])
    d = RemoteSourceDiscovery([b1, b2])
    run(d.aclose())
    assert b1.closed and b2.closed


def test_factory_functions_build_without_connecting() -> None:
    from yai_core.integrations.mcp.client import McpServerConfig
    from yai_core.integrations.openapi.spec import OpenApiSpecConfig

    d1 = mcp_discovery([McpServerConfig(alias="x", url="https://e/mcp")])
    assert len(d1._bridges) == 1 and d1._cache is None

    d2 = openapi_discovery(
        [OpenApiSpecConfig(spec={"openapi": "3.0.0", "paths": {}})]
    )
    assert len(d2._bridges) == 1 and d2._cache is None


def _spec_desc(name: str, description: str, source: str = "mcp") -> ToolSpec:
    return ToolSpec(
        name=name, description=description, input_schema={}, handler=None, source=source
    )


def test_filter_narrows_to_relevant() -> None:
    weather = _spec_desc("get_weather", "查询指定城市的实时天气、气温、降水概率")
    order = _spec_desc("create_order", "为客户创建一张销售订单并记录金额")
    d = RemoteSourceDiscovery([FakeBridge([weather, order])])
    out = run(
        d.discover(
            "需要一个能查询天气的工具",
            task="明天出门要不要带伞，会不会下雨",
            available=[],
        )
    )
    assert [s.name for s in out] == ["get_weather"]
    assert d.last_narrowed is True
    assert d.last_scores["get_weather"] > d.last_scores["create_order"]


def test_filter_falls_back_when_none_relevant() -> None:
    a = _spec_desc("alpha", "整理仓库库存清单")
    b = _spec_desc("beta", "生成季度财务报表")
    d = RemoteSourceDiscovery([FakeBridge([a, b])])
    out = run(
        d.discover(
            "需要一个能订机票的工具",
            task="帮我订一张去北京的机票",
            available=[],
        )
    )
    assert [s.name for s in out] == ["alpha", "beta"]
    assert d.last_narrowed is False


def test_filter_disabled_returns_all() -> None:
    weather = _spec_desc("get_weather", "查询指定城市的实时天气、气温")
    order = _spec_desc("create_order", "为客户创建一张销售订单")
    d = RemoteSourceDiscovery([FakeBridge([weather, order])], filter_by_need=False)
    out = run(d.discover("需要查询天气", task="明天下雨吗", available=[]))
    assert {s.name for s in out} == {"get_weather", "create_order"}
    assert d.last_narrowed is False


def test_top_k_limits_relevant() -> None:
    specs = [
        _spec_desc(f"weather_{i}", "查询城市实时天气、气温与降水") for i in range(4)
    ]
    d = RemoteSourceDiscovery([FakeBridge(specs)], top_k=2)
    out = run(d.discover("需要查询天气", task="明天会不会下雨", available=[]))
    assert len(out) == 2
    assert d.last_narrowed is True


class FakeEmbedder:
    """按文本是否含"天气"返回两类正交向量，模拟语义通道；零网络。"""

    async def embed_texts(self, texts):
        return [
            [1.0, 0.0, 0.0, 0.0] if "天气" in t else [0.0, 1.0, 0.0, 0.0]
            for t in texts
        ]


class BadEmbedder:
    async def embed_texts(self, texts):
        raise RuntimeError("embedder down")


def test_semantic_channel_narrows_cross_concept() -> None:
    # 描述用近义表述（大气湿度/降水），词法弱、语义强，语义通道应识别并收窄。
    weather = _spec_desc("get_weather", "获取大气湿度与降水概率")
    order = _spec_desc("create_order", "为客户创建一张销售订单")
    d = RemoteSourceDiscovery([FakeBridge([weather, order])], embedder=FakeEmbedder())
    out = run(d.discover("需要查询天气", task="明天下雨吗", available=[]))
    assert [s.name for s in out] == ["get_weather"]
    assert d.last_narrowed is True


def test_semantic_failure_falls_back_to_lexical() -> None:
    weather = _spec_desc("get_weather", "查询指定城市的实时天气、气温、降水概率")
    order = _spec_desc("create_order", "为客户创建一张销售订单")
    d = RemoteSourceDiscovery([FakeBridge([weather, order])], embedder=BadEmbedder())
    out = run(
        d.discover(
            "需要一个能查询天气的工具",
            task="明天出门要不要带伞，会不会下雨",
            available=[],
        )
    )
    assert [s.name for s in out] == ["get_weather"]  # 词法通道仍收窄
    assert d.last_narrowed is True
