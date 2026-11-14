"""父子核 Phase 2（本机 IPC）回归：发现文件 → 握手 → grant → 调用 → 失败关闭。

全部离线：只走 127.0.0.1 + 临时目录里的发现文件，不依赖网络与模型。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from shell.federation import (
    ChildCore,
    ChildServer,
    CoreGrant,
    DiscoveryInfo,
    Federation,
    GrantDenied,
    LinkChild,
)

from yai_core import (
    AgentCore,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    build_spec,
)


def _document_child():
    written: list[tuple[str, str]] = []

    def add_entry(title: str, body: str = "") -> str:
        """向文档追加一条记录。"""
        written.append((title, body))
        return f"已写入 {title}"

    def list_entries() -> list[str]:
        """列出文档记录标题。"""
        return [title for title, _ in written]

    def slow() -> str:
        """慢工具（验证 deadline）。"""
        import time as _time

        _time.sleep(0.5)
        return "慢工具完成"

    core = AgentCore(ScriptedModel([ModelResponse(content="idle")]))
    core.register_tools([build_spec(add_entry), build_spec(list_entries), build_spec(slow)])
    return ChildCore(core, node_id="doc", child_id="doc-app@0.29", product="doc"), written


def _server(tmp_path: Path, child, *, parent_tools=None) -> ChildServer:
    """parent_tools=None = 任何能出示 token 的父核都接受（仅用于测试/演示）。"""
    return ChildServer(
        child,
        discovery_dir=tmp_path,
        parent_tools=parent_tools,
        allow_any_parent=parent_tools is None,
    )


def _grant(link: LinkChild, tools) -> CoreGrant:
    return CoreGrant(edge="atm->doc", child_id=link.child_id, tools=frozenset(tools))


def test_in_process_server_link_roundtrip(tmp_path: Path) -> None:
    child, written = _document_child()

    async def main():
        server = _server(tmp_path, child)
        await server.start()
        assert server.discovery_path is not None and server.discovery_path.is_file()
        link = await LinkChild.connect(server.discovery_path, parent_id="atm")
        try:
            assert link.node_id == "doc"
            assert set(link.manifest()) >= {"add_entry", "list_entries"}
            result = await link.call(
                "add_entry",
                {"title": "IPC 流水", "body": "100 元"},
                grant=_grant(link, ["add_entry"]),
            )
            health = await link.health()
        finally:
            await link.close()
            await server.stop()
        return result, health

    result, health = asyncio.run(main())
    assert result["ok"] is True
    assert written == [("IPC 流水", "100 元")]
    assert health["ok"] is True and health["node_id"] == "doc"


def test_handshake_rejects_wrong_token(tmp_path: Path) -> None:
    child, _written = _document_child()

    async def main():
        server = _server(tmp_path, child)
        await server.start()
        try:
            # 篡改发现文件里的 token，模拟"另一个进程拿着错凭证"
            import json

            data = json.loads(server.discovery_path.read_text(encoding="utf-8"))
            data["token"] = "wrong"
            server.discovery_path.write_text(json.dumps(data), encoding="utf-8")
            with pytest.raises(GrantDenied):
                await LinkChild.connect(server.discovery_path, parent_id="atm")
        finally:
            await server.stop()

    asyncio.run(main())


def test_handshake_rejects_unlisted_parent(tmp_path: Path) -> None:
    child, _written = _document_child()

    async def main():
        server = _server(tmp_path, child, parent_tools={"atm": frozenset({"add_entry"})})
        await server.start()
        link = None
        try:
            with pytest.raises(GrantDenied):
                link = await LinkChild.connect(server.discovery_path, parent_id="evil")
        finally:
            if link is not None:
                await link.close()
            await server.stop()

    asyncio.run(main())


def test_server_grant_caps_the_tool_subset(tmp_path: Path) -> None:
    child, _written = _document_child()

    async def main():
        server = _server(tmp_path, child)
        await server.start()
        # 父核只握手要 add_entry，但本地 grant 假装也能调 list_entries
        link = await LinkChild.connect(
            server.discovery_path, parent_id="atm", tools=["add_entry"]
        )
        try:
            with pytest.raises(GrantDenied):
                await link.call(
                    "list_entries",
                    {},
                    grant=_grant(link, ["add_entry", "list_entries"]),
                )
        finally:
            await link.close()
            await server.stop()

    asyncio.run(main())


def test_deadline_exceeded_maps_to_child_failure(tmp_path: Path) -> None:
    """父核侧 call_timeout 超时 → 工具失败回流，不挂死、不拖垮父核。"""
    child, _written = _document_child()

    async def main():
        server = _server(tmp_path, child)
        await server.start()
        link = await LinkChild.connect(server.discovery_path, parent_id="atm")
        parent = AgentCore(
            ScriptedModel(
                [
                    ModelResponse(
                        content="",
                        tool_calls=[ToolCallRequest(id="c1", name="doc__slow", arguments={})],
                    ),
                    ModelResponse(content="慢工具超时了。"),
                ]
            )
        )
        federation = Federation(parent, node_id="atm", call_timeout=0.05)
        federation.register_child(link, tools=["slow"])
        try:
            return await parent.run("调用慢工具")
        finally:
            await link.close()
            await server.stop()

    result = asyncio.run(main())
    assert result.final_text == "慢工具超时了。"


def test_federation_over_ipc_end_to_end(tmp_path: Path) -> None:
    child, written = _document_child()

    async def main():
        server = _server(tmp_path, child)
        await server.start()
        link = await LinkChild.connect(server.discovery_path, parent_id="atm")
        parent = AgentCore(
            ScriptedModel(
                [
                    ModelResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="c1",
                                name="doc__add_entry",
                                arguments={"title": "ATM 流水 0002", "body": "转出 200 元"},
                            )
                        ],
                    ),
                    ModelResponse(content="已通过 IPC 记入文档。"),
                ]
            )
        )
        federation = Federation(parent, node_id="atm")
        federation.register_child(link, tools=["add_entry"])
        try:
            return await parent.run("把流水记进文档")
        finally:
            await link.close()
            await server.stop()

    result = asyncio.run(main())
    assert result.final_text == "已通过 IPC 记入文档。"
    assert written == [("ATM 流水 0002", "转出 200 元")]


def test_ipc_demo_runs_two_processes_offline() -> None:
    """真实双进程：子核 app 由 demo 拉起，退出后必须失败关闭。"""
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(root / "examples" / "host_m_federation" / "ipc_demo.py"),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    output = completed.stdout
    assert "[IPC] 连上子核 doc" in output
    assert "[IPC] 调用成功" in output
    assert "子核退出后调用失败（预期）" in output


def test_subject_is_bound_to_handshake_grant(tmp_path: Path) -> None:
    child, written = _document_child()

    async def main():
        server = _server(tmp_path, child)
        await server.start()
        link = await LinkChild.connect(
            server.discovery_path,
            parent_id="atm",
            subject={"id": "alice"},
        )
        try:
            grant = CoreGrant(
                edge="atm->doc",
                child_id=link.child_id,
                tools=frozenset({"add_entry"}),
                subject={"id": "alice"},
            )
            ok = await link.call(
                "add_entry",
                {"title": "alice 的记录"},
                grant=grant,
                subject={"id": "alice"},
            )
            with pytest.raises(GrantDenied):
                await link.call(
                    "add_entry",
                    {"title": "bob 的记录"},
                    grant=grant,
                    subject={"id": "bob"},
                )
            return ok
        finally:
            await link.close()
            await server.stop()

    result = asyncio.run(main())
    assert result["ok"] is True
    assert written == [("alice 的记录", "")]


def test_child_server_requires_explicit_parent_allowlist() -> None:
    child, _written = _document_child()
    with pytest.raises(ValueError, match="parent_tools"):
        ChildServer(child)


def test_discovery_rejects_path_traversal(tmp_path: Path) -> None:
    info = DiscoveryInfo(
        node_id="../escape",
        child_id="x@1",
        host="127.0.0.1",
        port=1,
        token="t",
        pid=1,
    )
    with pytest.raises(ValueError, match="node_id"):
        info.write(tmp_path)
