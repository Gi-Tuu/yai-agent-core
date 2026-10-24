"""Sidecar 控制协议：初始化、运行、审批/澄清、取消、错误与清理（全离线）。"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from yai_core import ModelResponse, ScriptedModel, ToolCallRequest, build_spec
from yai_core.sidecar.channel import SidecarChannel
from yai_core.sidecar.protocol import (
    ALREADY_INITIALIZED,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    NOT_INITIALIZED,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    RUN_BUSY,
    RpcError,
    jsonable,
)
from yai_core.sidecar.server import SidecarServer, attach_configured_tools


def async_test(fn):
    """项目不依赖 pytest-asyncio：用 asyncio.run 驱动每个用例。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


async def _no_tools(core, params):  # noqa: ANN001, ARG001
    return []


class Harness:
    """在进程内喂 JSON-RPC 消息，收集 sidecar 发出的响应/通知。"""

    def __init__(self, *, model_factory, tool_attacher=None) -> None:
        self.sent: list[dict[str, Any]] = []
        self._next_id = 1
        self.server = SidecarServer(
            send=self._send,
            model_factory=model_factory,
            tool_attacher=tool_attacher or _no_tools,
        )

    async def _send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        await self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        for message in reversed(self.sent):
            if message.get("id") == request_id and (
                "result" in message or "error" in message
            ):
                return message
        raise AssertionError(f"没有收到 {method} 的响应")

    async def wait_for(self, predicate, *, timeout: float = 3.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for message in self.sent:
                if predicate(message):
                    return message
            await asyncio.sleep(0.01)
        received = json.dumps(self.sent, ensure_ascii=False)[:800]
        raise AssertionError(f"等待消息超时；已收到：{received}")

    async def respond(self, request: dict[str, Any], result: Any) -> None:
        await self.server.handle(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        )

    async def close(self) -> None:
        if not self.server._closed:
            await self.call("shutdown")


def _events(harness: Harness) -> list[dict[str, Any]]:
    return [
        message
        for message in harness.sent
        if message.get("method") == "agent/event"
    ]


async def _wait_done(harness: Harness) -> dict[str, Any]:
    return await harness.wait_for(
        lambda m: m.get("method") == "agent/event"
        and m["params"]["type"] == "done"
    )


@async_test
async def test_initialize_returns_versions_and_tools() -> None:
    harness = Harness(model_factory=lambda cfg: ScriptedModel([ModelResponse(content="ok")]))
    try:
        reply = await harness.call("initialize", {"model": {}})
        result = reply["result"]
        assert result["protocol_version"] == PROTOCOL_VERSION
        assert result["kernel_version"]
        assert result["tools"] == []
        assert result["capabilities"]["single_run"] is True
    finally:
        await harness.close()


@async_test
async def test_run_streams_events_and_done() -> None:
    harness = Harness(
        model_factory=lambda cfg: ScriptedModel([ModelResponse(content="你好")])
    )
    try:
        await harness.call("initialize", {"model": {}})
        reply = await harness.call("agent/run", {"task": "你好"})
        run_id = reply["result"]["run_id"]
        done = await _wait_done(harness)
        assert done["params"]["run_id"] == run_id
        assert done["params"]["data"]["final_text"] == "你好"
        assert any(
            m["params"]["type"] == "strategy_selected" for m in _events(harness)
        )
    finally:
        await harness.close()


@async_test
async def test_tool_approval_is_forwarded_and_can_be_approved() -> None:
    calls: list[str] = []

    def lookup(name: str = "") -> dict:
        """按姓名查询客户。"""
        calls.append(name)
        return {"name": name}

    async def attach(core, params):  # noqa: ANN001, ARG001
        core.register_tools([build_spec(lookup)])
        return []

    harness = Harness(
        model_factory=lambda cfg: ScriptedModel(
            [
                ModelResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="c1", name="lookup", arguments={"name": "王敏"}
                        )
                    ],
                ),
                ModelResponse(content="已查询"),
            ]
        ),
        tool_attacher=attach,
    )
    try:
        await harness.call("initialize", {"model": {}})
        await harness.call("agent/run", {"task": "查一下客户王敏"})

        ask = await harness.wait_for(
            lambda m: m.get("method") == "approval/request"
        )
        assert ask["params"]["tool"] == "lookup"
        assert ask["params"]["arguments"] == {"name": "王敏"}
        await harness.respond(ask, {"approved": True})

        done = await _wait_done(harness)
        assert calls == ["王敏"]
        assert done["params"]["data"]["final_text"] == "已查询"
        assert any(
            m["params"]["type"] == "tool_result"
            and m["params"]["data"].get("ok") is True
            for m in _events(harness)
        )
    finally:
        await harness.close()


@async_test
async def test_tool_approval_denied_does_not_execute() -> None:
    calls: list[str] = []

    def lookup(name: str = "") -> dict:
        """按姓名查询客户。"""
        calls.append(name)
        return {"name": name}

    async def attach(core, params):  # noqa: ANN001, ARG001
        core.register_tools([build_spec(lookup)])
        return []

    harness = Harness(
        model_factory=lambda cfg: ScriptedModel(
            [
                ModelResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="c1", name="lookup", arguments={"name": "王敏"}
                        )
                    ],
                ),
                ModelResponse(content="未获授权，未查询"),
            ]
        ),
        tool_attacher=attach,
    )
    try:
        await harness.call("initialize", {"model": {}})
        await harness.call("agent/run", {"task": "查一下客户王敏"})
        ask = await harness.wait_for(
            lambda m: m.get("method") == "approval/request"
        )
        await harness.respond(ask, {"approved": False})
        done = await _wait_done(harness)
        assert calls == []
        assert done["params"]["data"]["final_text"] == "未获授权，未查询"
        assert not any(m["params"]["type"] == "tool_call" for m in _events(harness))
    finally:
        await harness.close()


@async_test
async def test_clarify_is_forwarded_to_host() -> None:
    harness = Harness(
        model_factory=lambda cfg: ScriptedModel(
            [ModelResponse(content="湛江今天晴，26℃。")]
        )
    )
    try:
        await harness.call("initialize", {"model": {}})
        await harness.call("agent/run", {"task": "随便"})
        ask = await harness.wait_for(
            lambda m: m.get("method") == "clarify/request"
        )
        assert ask["params"]["question"]
        await harness.respond(ask, {"answer": "查一下湛江天气"})
        done = await _wait_done(harness)
        assert done["params"]["data"]["final_text"] == "湛江今天晴，26℃。"
    finally:
        await harness.close()


@async_test
async def test_cancel_stops_active_run() -> None:
    async def slow() -> dict:
        """慢工具。"""
        await asyncio.sleep(5)
        return {"ok": True}

    async def attach(core, params):  # noqa: ANN001, ARG001
        core.register_tools([build_spec(slow)])
        return []

    harness = Harness(
        model_factory=lambda cfg: ScriptedModel(
            [
                ModelResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(id="c1", name="slow", arguments={})
                    ],
                ),
                ModelResponse(content="完成"),
            ]
        ),
        tool_attacher=attach,
    )
    try:
        await harness.call(
            "initialize", {"model": {}, "policy": {"mode": "allow_all"}}
        )
        reply = await harness.call("agent/run", {"task": "调用慢工具"})
        run_id = reply["result"]["run_id"]
        # 工具事件由执行器在 handler 结束后一次性返回；这里等到任务真正在跑即可。
        await harness.wait_for(
            lambda m: m.get("method") == "agent/event"
            and m["params"]["type"] == "tier_changed"
        )
        assert harness.server._active_run_id == run_id
        cancel = await harness.call("agent/cancel", {"run_id": run_id})
        assert cancel["result"]["cancelled"] is True
        cancelled = await harness.wait_for(
            lambda m: m.get("method") == "agent/cancelled"
        )
        assert cancelled["params"]["run_id"] == run_id
    finally:
        await harness.close()


@async_test
async def test_protocol_errors_and_single_run_guard() -> None:
    async def slow() -> dict:
        """慢工具。"""
        await asyncio.sleep(5)
        return {"ok": True}

    async def attach(core, params):  # noqa: ANN001, ARG001
        core.register_tools([build_spec(slow)])
        return []

    harness = Harness(
        model_factory=lambda cfg: ScriptedModel(
            [
                ModelResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="c1", name="slow", arguments={})],
                ),
                ModelResponse(content="完成"),
            ]
        ),
        tool_attacher=attach,
    )
    try:
        before = await harness.call("agent/run", {"task": "你好"})
        assert before["error"]["code"] == NOT_INITIALIZED
        pong = await harness.call("ping")
        assert pong["result"]["pong"] is True

        await harness.call("initialize", {"model": {}})
        second = await harness.call("initialize", {"model": {}})
        assert second["error"]["code"] == ALREADY_INITIALIZED

        unknown = await harness.call("nope")
        assert unknown["error"]["code"] == METHOD_NOT_FOUND

        bad = await harness.call("agent/run", {"task": " "})
        assert bad["error"]["code"] == INVALID_PARAMS

        await harness.call(
            "agent/run", {"task": "调用慢工具"}
        )
        busy = await harness.call("agent/run", {"task": "再来一个"})
        assert busy["error"]["code"] == RUN_BUSY

        await harness.server.handle_line(b"{not json")
        parse_error = await harness.wait_for(
            lambda m: m.get("error", {}).get("code") == PARSE_ERROR
        )
        assert parse_error["id"] is None
    finally:
        await harness.close()


@async_test
async def test_channel_variants_and_timeout() -> None:
    calls: list[tuple[str, dict]] = []

    async def request(method, params):
        calls.append((method, params))
        return {"approved": True} if method == "approval/request" else {"answer": "补充"}

    channel = SidecarChannel(request)
    await channel.emit(None)
    assert await channel.confirm("t", {"x": 1}, {"risk": "high"}) is True
    assert await channel.ask("q") == "补充"
    assert calls[0][1]["context"] == {"risk": "high"}

    async def returns_bool(method, params):  # noqa: ANN001, ARG001
        return True

    assert await SidecarChannel(returns_bool).confirm("t", {}) is True

    async def slow(method, params):  # noqa: ANN001, ARG001
        await asyncio.sleep(1)
        return True

    timed_out = SidecarChannel(slow, timeout=0.01)
    assert await timed_out.confirm("t", {}) is False
    assert await timed_out.ask("q") == ""


def test_jsonable_and_rpc_error() -> None:
    @dataclass
    class Sample:
        value: int

    assert jsonable(Sample(1)) == {"value": 1}
    assert jsonable(b"abc") == "<3 bytes>"
    assert jsonable((1, 2)) == [1, 2]
    error = RpcError(INVALID_REQUEST, "bad", {"field": "x"})
    assert error.to_dict() == {
        "code": INVALID_REQUEST,
        "message": "bad",
        "data": {"field": "x"},
    }


@async_test
async def test_initialize_validation_and_tool_failure_cleanup() -> None:
    harness = Harness(
        model_factory=lambda cfg: ScriptedModel([ModelResponse(content="ok")])
    )
    try:
        bad_policy = await harness.call("initialize", {"model": {}, "policy": "x"})
        assert bad_policy["error"]["code"] == INVALID_PARAMS
        bad_timeout = await harness.call(
            "initialize", {"model": {}, "interaction_timeout": 0}
        )
        assert bad_timeout["error"]["code"] == INVALID_PARAMS
        ok = await harness.call("initialize", {"model": {}})
        assert "result" in ok
        bad_subject = await harness.call(
            "agent/run", {"task": "你好", "subject": {}}
        )
        assert bad_subject["error"]["code"] == INVALID_PARAMS
    finally:
        await harness.close()

    def bad_model(config):  # noqa: ANN001, ARG001
        raise ValueError("bad model")

    bad_model_harness = Harness(model_factory=bad_model)
    reply = await bad_model_harness.call("initialize", {"model": {}})
    assert reply["error"]["code"] == INVALID_PARAMS
    await bad_model_harness.close()

    async def fail_tools(core, params):  # noqa: ANN001, ARG001
        raise ValueError("bad tools")

    bad_tools = Harness(
        model_factory=lambda cfg: ScriptedModel([ModelResponse(content="ok")]),
        tool_attacher=fail_tools,
    )
    reply = await bad_tools.call("initialize", {"model": {}})
    assert reply["error"]["code"] == INVALID_PARAMS
    await bad_tools.close()


@async_test
async def test_invalid_messages_and_cleanup_survives_bridge_errors() -> None:
    sent: list[dict[str, Any]] = []
    server = SidecarServer(
        send=lambda message: _append(sent, message),
        model_factory=lambda cfg: ScriptedModel([ModelResponse(content="ok")]),
    )
    await server.handle("not-an-object")
    await server.handle({"jsonrpc": "1.0", "id": 1, "method": "ping"})
    await server.handle({"jsonrpc": "2.0", "id": 1, "foo": 1})
    await server.handle({"jsonrpc": "2.0", "id": "unknown", "result": 1})
    assert sent[0]["error"]["code"] == INVALID_REQUEST
    assert sent[1]["error"]["code"] == INVALID_REQUEST
    assert sent[2]["error"]["code"] == INVALID_REQUEST
    assert len(sent) == 3  # 未知 id 的响应被忽略，不回复

    shutdown = await server.handle({"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
    assert shutdown is None  # handle 自己回响应
    assert server._closed is True


async def _append(sent: list[dict[str, Any]], message: dict[str, Any]) -> None:
    sent.append(message)


@async_test
async def test_shutdown_before_initialize_and_bridge_close_error_is_suppressed() -> None:
    class BadBridge:
        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    async def attach(core, params):  # noqa: ANN001, ARG001
        return [BadBridge()]

    harness = Harness(
        model_factory=lambda cfg: ScriptedModel([ModelResponse(content="ok")]),
        tool_attacher=attach,
    )
    await harness.call("initialize", {"model": {}})
    reply = await harness.call("shutdown")
    assert reply["result"] == {"stopped": True}

    fresh = Harness(
        model_factory=lambda cfg: ScriptedModel([ModelResponse(content="ok")])
    )
    reply = await fresh.call("shutdown")
    assert reply["result"] == {"stopped": True}


def test_cli_entrypoint_ndjson() -> None:
    pytest.importorskip("openai")  # 默认模型工厂需要 [llm] extra
    payload = "\n".join(
        [
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"model":{}}}',
            '{"jsonrpc":"2.0","id":2,"method":"ping"}',
            '{"jsonrpc":"2.0","id":3,"method":"shutdown"}',
            "",
        ]
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "yai_core.sidecar"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line]
    assert lines[0]["result"]["protocol_version"] == PROTOCOL_VERSION
    assert lines[1]["result"]["pong"] is True
    assert lines[2]["result"] == {"stopped": True}


@async_test
async def test_mcp_tools_are_attached_and_callable() -> None:
    pytest.importorskip("mcp")
    demo_server = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "host_d_mcp"
        / "demo_mcp_server.py"
    )
    harness = Harness(
        model_factory=lambda cfg: ScriptedModel(
            [
                ModelResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(id="c1", name="add", arguments={"a": 17, "b": 25})
                    ],
                ),
                ModelResponse(content="42"),
            ]
        ),
        tool_attacher=attach_configured_tools,
    )
    try:
        init = await harness.call(
            "initialize",
            {
                "model": {},
                "policy": {"mode": "allow_all"},
                "mcp": [
                    {
                        "alias": "demo",
                        "command": sys.executable,
                        "args": [str(demo_server)],
                    }
                ],
            },
        )
        names = {tool["name"] for tool in init["result"]["tools"]}
        assert {"add", "weather"} <= names

        await harness.call("agent/run", {"task": "用工具算一下 17 加 25"})
        done = await _wait_done(harness)
        assert done["params"]["data"]["final_text"] == "42"
        assert any(
            "42" in json.dumps(m["params"]["data"], ensure_ascii=False)
            for m in _events(harness)
            if m["params"]["type"] == "tool_result"
        )
    finally:
        await harness.close()


@async_test
async def test_openapi_tools_are_attached_from_inline_spec() -> None:
    pytest.importorskip("httpx")
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Demo", "version": "1.0"},
        "servers": [{"url": "http://127.0.0.1:9"}],
        "paths": {
            "/items": {
                "get": {
                    "operationId": "list_items",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    harness = Harness(
        model_factory=lambda cfg: ScriptedModel([ModelResponse(content="ok")]),
        tool_attacher=attach_configured_tools,
    )
    try:
        init = await harness.call(
            "initialize",
            {
                "model": {},
                "openapi": [
                    {"alias": "demo", "spec": spec, "read_only": True}
                ],
            },
        )
        names = {tool["name"] for tool in init["result"]["tools"]}
        assert any("list" in name.lower() for name in names), names
    finally:
        await harness.close()


@async_test
async def test_shutdown_closes_attached_bridges() -> None:
    closed: list[bool] = []

    class Bridge:
        async def aclose(self) -> None:
            closed.append(True)

    async def attach(core, params):  # noqa: ANN001, ARG001
        return [Bridge()]

    harness = Harness(
        model_factory=lambda cfg: ScriptedModel([ModelResponse(content="ok")]),
        tool_attacher=attach,
    )
    await harness.call("initialize", {"model": {}})
    reply = await harness.call("shutdown")
    assert reply["result"] == {"stopped": True}
    assert closed == [True]
    assert harness.server._closed is True
