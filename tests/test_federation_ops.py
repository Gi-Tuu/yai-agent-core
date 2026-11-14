"""父子核 Phase 3（生态运维）回归：manifest 热更新、每 edge 配额/统计、子核生命周期托管。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from shell.federation import (
    ChildCallFailed,
    ChildCore,
    ChildServer,
    ChildUnavailable,
    CoreGrant,
    Federation,
    FederationError,
    LinkChild,
    ManagedChild,
)

from yai_core import AgentCore, ModelResponse, ScriptedModel, build_spec

ROOT = Path(__file__).resolve().parents[1]
CHILD_APP = ROOT / "examples" / "host_m_federation" / "child_app.py"


def _core_with(*functions):
    core = AgentCore(ScriptedModel([ModelResponse(content="idle")]))
    if functions:
        core.register_tools([build_spec(fn) for fn in functions])
    return core


def _child(*functions) -> ChildCore:
    return ChildCore(_core_with(*functions), node_id="doc", child_id="doc-app", product="doc")


def _grant(link: LinkChild) -> CoreGrant:
    return CoreGrant(
        edge="atm->doc",
        child_id=link.child_id,
        tools=frozenset(link.manifest()),
    )


# ---------- manifest 热更新 ----------


def test_manifest_hot_update_in_process() -> None:
    def add_entry(title: str = "") -> str:
        """写一条。"""
        return f"add:{title}"

    child = _child(add_entry)
    parent = _core_with()
    federation = Federation(parent, node_id="atm")
    federation.register_child(child, tools=None)

    def sum_numbers(numbers: list[int]) -> int:
        """求和。"""
        return sum(numbers)

    child.core.register_tools([build_spec(sum_numbers)])
    outcome = asyncio.run(federation.refresh_child("doc"))

    assert outcome["added"] == ["sum_numbers"]
    assert "doc__sum_numbers" in {tool["name"] for tool in parent.list_tools()}

    # 子核移除工具 → 父核收敛掉暴露项
    child.core.registry.unregister("add_entry")
    outcome = asyncio.run(federation.refresh_child("doc"))
    assert outcome["removed"] == ["add_entry"]
    assert "doc__add_entry" not in {tool["name"] for tool in parent.list_tools()}


def test_manifest_hot_update_over_ipc(tmp_path: Path) -> None:
    def add_entry(title: str = "") -> str:
        """写一条。"""
        return f"add:{title}"

    child = _child(add_entry)

    async def main():
        server = ChildServer(child, discovery_dir=tmp_path, allow_any_parent=True)
        await server.start()
        link = await LinkChild.connect(server.discovery_path, parent_id="atm")
        try:
            def ping() -> str:
                """健康探针。"""
                return "pong"

            child.core.register_tools([build_spec(ping)])
            changed = await link.refresh_manifest()
            assert changed is True
            assert "ping" in link.manifest()
            result = await link.call("ping", {}, grant=CoreGrant(
                edge="atm->doc", child_id=link.child_id, tools=frozenset(link.manifest())
            ))
            assert result["ok"] is True
        finally:
            await link.close()
            await server.stop()

    asyncio.run(main())


# ---------- 每 edge 配额与统计 ----------


def test_server_edge_quota_fails_closed(tmp_path: Path) -> None:
    def echo(text: str = "") -> str:
        """回显。"""
        return text

    child = _child(echo)

    async def main():
        server = ChildServer(
            child,
            discovery_dir=tmp_path,
            allow_any_parent=True,
            edge_quota_per_minute=2,
        )
        await server.start()
        link = await LinkChild.connect(server.discovery_path, parent_id="atm")
        try:
            for index in range(2):
                result = await link.call(
                    "echo", {"text": str(index)}, grant=_grant(link)
                )
                assert result["ok"] is True
            with pytest.raises(ChildCallFailed) as excinfo:
                await link.call("echo", {"text": "third"}, grant=_grant(link))
            assert "quota" in str(excinfo.value).lower()
        finally:
            await link.close()
            await server.stop()

    asyncio.run(main())


def test_parent_edge_budget_and_stats() -> None:
    def add_entry(title: str = "") -> str:
        """写一条。"""
        return f"add:{title}"

    child = _child(add_entry)
    parent = _core_with()
    federation = Federation(parent, node_id="atm")
    federation.register_child(child, max_calls=2)

    async def main():
        await federation.invoke("doc", "add_entry", {"title": "a"})
        await federation.invoke("doc", "add_entry", {"title": "b"})
        with pytest.raises(FederationError) as excinfo:
            await federation.invoke("doc", "add_entry", {"title": "c"})
        return str(excinfo.value)

    message = asyncio.run(main())
    assert "edge_budget_exceeded" in message
    stats = federation.edge_stats("doc")
    assert stats["calls"] == 2
    assert stats["total_ms"] > 0


def test_failed_calls_counted_in_stats() -> None:
    def boom() -> str:
        """总是失败。"""
        raise RuntimeError("子核内部错误")

    child = _child(boom)
    parent = _core_with()
    federation = Federation(parent, node_id="atm")
    federation.register_child(child)

    with pytest.raises(ChildCallFailed):
        asyncio.run(federation.invoke("doc", "boom"))

    stats = federation.edge_stats("doc")
    assert stats["calls"] == 1
    assert stats["failures"] == 1
    assert stats["last_error"]  # 记类型与原因；参数值不进统计


def test_unregister_child_removes_tools() -> None:
    def add_entry(title: str = "") -> str:
        """写一条。"""
        return title

    child = _child(add_entry)
    parent = _core_with()
    federation = Federation(parent, node_id="atm")
    federation.register_child(child, prefix="doc")

    assert federation.unregister_child("doc") is True
    assert federation.unregister_child("doc") is False
    assert "doc__add_entry" not in {tool["name"] for tool in parent.list_tools()}
    with pytest.raises(FederationError):
        asyncio.run(federation.invoke("doc", "add_entry", {"title": "x"}))


# ---------- 进程生命周期托管 ----------


def _managed(tmp_path: Path) -> ManagedChild:
    command = [
        sys.executable,
        "-X",
        "utf8",
        str(CHILD_APP),
        "--discovery-dir",
        str(tmp_path),
        "--parent-id",
        "atm",
    ]
    return ManagedChild(
        command,
        discovery_dir=tmp_path,
        node_id="doc",
        parent_id="atm",
        connect_timeout=25.0,
    )


def test_managed_child_start_call_stop(tmp_path: Path) -> None:
    async def main():
        managed = _managed(tmp_path)
        await managed.start()
        try:
            result = await managed.call("add_entry", {"title": "托管流水", "body": "100"})
            assert result["ok"] is True
        finally:
            await managed.stop()
        return managed

    managed = asyncio.run(main())
    assert managed.alive() is False
    assert not managed.discovery_path.exists()
    with pytest.raises(ChildUnavailable):
        asyncio.run(managed.ensure())


def test_managed_child_fails_closed_when_process_dies(tmp_path: Path) -> None:
    async def main():
        managed = _managed(tmp_path)
        await managed.start()
        try:
            assert (await managed.ensure()).node_id == "doc"
            assert managed.process is not None
            managed.process.kill()
            await asyncio.to_thread(managed.process.wait, 10)
            with pytest.raises(ChildUnavailable):
                await managed.ensure()
        finally:
            await managed.stop()

    asyncio.run(main())


def test_quota_cannot_be_reset_by_rotating_parent_id(tmp_path: Path) -> None:
    def echo(text: str = "") -> str:
        """回显。"""
        return text

    child = _child(echo)

    async def main() -> None:
        server = ChildServer(
            child,
            discovery_dir=tmp_path,
            allow_any_parent=True,
            edge_quota_per_minute=1,
        )
        await server.start()
        first = await LinkChild.connect(server.discovery_path, parent_id="p1")
        second = await LinkChild.connect(server.discovery_path, parent_id="p2")
        try:
            assert (await first.call("echo", {}, grant=_grant(first)))["ok"] is True
            with pytest.raises(ChildCallFailed, match="quota"):
                await second.call("echo", {}, grant=_grant(second))
        finally:
            await first.close()
            await second.close()
            await server.stop()

    asyncio.run(main())
