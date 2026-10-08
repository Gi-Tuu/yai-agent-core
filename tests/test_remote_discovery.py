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
