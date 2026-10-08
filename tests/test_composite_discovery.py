"""CompositeDiscovery 测试：多源聚合、跨源去重、available 传递与声明目录聚合。"""

from __future__ import annotations

import asyncio

from yai_core.discovery.aggregate import CompositeDiscovery
from yai_core.types import ToolSpec


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name, description=name, input_schema={}, handler=None, source="native"
    )


class FakeDiscovery:
    def __init__(self, specs: list[ToolSpec], *, catalog=None) -> None:
        self._specs = specs
        self._catalog = catalog
        self.received_available: list[list[str]] = []

    async def discover(self, need, *, task, available):
        self.received_available.append(list(available))
        avail = set(available)
        return [s for s in self._specs if s.name not in avail]

    def list_discoverable(self):
        return self._catalog or []


class NoCatalogDiscovery:
    async def discover(self, need, *, task, available):
        return []


def run(coro):
    return asyncio.run(coro)


def test_aggregates_and_passes_growing_available() -> None:
    d1 = FakeDiscovery([_spec("a"), _spec("b")])
    d2 = FakeDiscovery([_spec("b"), _spec("c")])  # b 跨源重名
    comp = CompositeDiscovery([d1, d2])
    out = run(comp.discover("need", task="t", available=["c"]))
    assert [s.name for s in out] == ["a", "b"]
    # d2 被调用时，available 已包含 d1 返回的 a、b 与初始的 c
    assert set(d2.received_available[0]) == {"a", "b", "c"}


def test_list_discoverable_merges_and_dedupes() -> None:
    d1 = FakeDiscovery([], catalog=[{"name": "a", "summary": "A"}])
    d2 = FakeDiscovery(
        [], catalog=[{"name": "a", "summary": "A2"}, {"name": "b", "summary": "B"}]
    )
    comp = CompositeDiscovery([d1, d2])
    assert comp.list_discoverable() == [
        {"name": "a", "summary": "A"},
        {"name": "b", "summary": "B"},
    ]


def test_list_discoverable_skips_sources_without_it() -> None:
    d1 = NoCatalogDiscovery()
    d2 = FakeDiscovery([], catalog=[{"name": "a", "summary": "A"}])
    comp = CompositeDiscovery([d1, d2])
    assert comp.list_discoverable() == [{"name": "a", "summary": "A"}]
