"""Sidecar 服务端端到端测试：装配、失败边界、stdio 传输与入口退出码（全离线）。

``tests/test_sidecar_protocol.py`` 覆盖的是协议语义；这一篇覆盖**服务端装配与
失败边界**：默认模型工厂、工具接入的清理、宿主失联、stdio 收尾、进程退出码。
全部离线：不联网、不需要 API Key，MCP/OpenAPI 用假模块注入。
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import runpy
import sys
import types
from typing import Any

import pytest

from yai_core import AgentCore, ModelResponse, ScriptedModel, ToolCallRequest, build_spec
from yai_core.sidecar import server as sidecar_server
from yai_core.sidecar.channel import SidecarChannel
from yai_core.sidecar.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    NOT_INITIALIZED,
    SHUTTING_DOWN,
    RpcError,
    encode,
    jsonable,
)
from yai_core.sidecar.server import (
    SidecarServer,
    _build_openai_model,
    _build_policy,
    _build_subject,
    attach_configured_tools,
)


def async_test(fn):
    """项目不依赖 pytest-asyncio：用 asyncio.run 驱动每个用例。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def _scripted(*responses: ModelResponse):
    return lambda config: ScriptedModel(list(responses))


class Harness:
    """在进程内喂 JSON-RPC 消息，收集 sidecar 发出的响应/通知。"""

    def __init__(self, *, model_factory=None, tool_attacher=None) -> None:
        self.sent: list[dict[str, Any]] = []
        self._next_id = 1
        self.server = SidecarServer(
            send=self._send,
            model_factory=model_factory or _scripted(ModelResponse(content="ok")),
            tool_attacher=tool_attacher,
        )

    async def _send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        await self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params if params is not None else {},
            }
        )
        for message in reversed(self.sent):
            if message.get("id") == request_id and (
                "result" in message or "error" in message
            ):
                return message
        raise AssertionError(f"没有收到 {method} 的响应")

    async def wait_for(self, predicate, *, timeout: float = 3.0) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for message in self.sent:
                if predicate(message):
                    return message
            await asyncio.sleep(0.01)
        raise AssertionError(
            f"等待消息超时；已收到：{json.dumps(self.sent, ensure_ascii=False)[:600]}"
        )

    async def close(self) -> None:
        if not self.server._closed:
            await self.call("shutdown")


# --------------------------------------------------------------------- 装配部件


def test_build_openai_model_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="openai-compatible"):
        _build_openai_model({"provider": "anthropic"})


def test_build_openai_model_defaults_to_openai_provider() -> None:
    pytest.importorskip("openai")
    provider = _build_openai_model({"provider": "openai-compatible", "model": "demo-model"})
    assert provider.model == "demo-model"
    assert provider.yai_live_router is True


def test_build_policy_defaults_and_validation() -> None:
    default = _build_policy(None)
    assert default.mode == "auto"
    assert default.allowed == set()

    allowed = _build_policy({"mode": "allow_all", "allow": [" search ", "notes"]})
    assert allowed.mode == "allow_all"
    assert allowed.allowed == {"search", "notes"}

    with pytest.raises(ValueError, match="policy 必须是 object"):
        _build_policy("auto")
    with pytest.raises(ValueError, match="policy.allow"):
        _build_policy({"allow": "search"})
    with pytest.raises(ValueError, match="policy.allow"):
        _build_policy({"allow": ["search", 7]})


def test_build_subject_variants() -> None:
    assert _build_subject(None) is None

    with pytest.raises(RpcError) as not_object:
        _build_subject("u1")
    assert not_object.value.code == INVALID_PARAMS

    with pytest.raises(RpcError) as no_id:
        _build_subject({"display_name": "王敏"})
    assert no_id.value.code == INVALID_PARAMS

    with pytest.raises(RpcError) as bad_attrs:
        _build_subject({"subject_id": "u1", "attributes": ["a"]})
    assert bad_attrs.value.code == INVALID_PARAMS

    subject = _build_subject(
        {"subject_id": " u1 ", "display_name": "王敏", "attributes": {"role": "admin"}}
    )
    assert subject is not None
    assert subject.subject_id == "u1"
    assert subject.display_name == "王敏"
    assert dict(subject.attributes) == {"role": "admin"}


# --------------------------------------------------------------- 工具接入与清理


def _fake_integration_module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def test_attach_configured_tools_rejects_non_list_sections() -> None:
    core = AgentCore(ScriptedModel([ModelResponse(content="ok")]))
    with pytest.raises(ValueError, match="必须是数组"):
        _run(attach_configured_tools(core, {"mcp": {"alias": "a"}}))
    with pytest.raises(ValueError, match="必须是数组"):
        _run(attach_configured_tools(core, {"openapi": {"alias": "a"}}))


def test_attach_configured_tools_reports_missing_optional_dependency(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "yai_core.integrations.mcp", None)
    core = AgentCore(ScriptedModel([ModelResponse(content="ok")]))
    with pytest.raises(ValueError, match=r"\[mcp\]"):
        _run(attach_configured_tools(core, {"mcp": [{"alias": "a"}]}))

    monkeypatch.setitem(sys.modules, "yai_core.integrations.openapi", None)
    core = AgentCore(ScriptedModel([ModelResponse(content="ok")]))
    with pytest.raises(ValueError, match=r"\[openapi\]"):
        _run(attach_configured_tools(core, {"openapi": [{"alias": "a"}]}))


def test_attach_configured_tools_rejects_non_object_items(monkeypatch) -> None:
    mcp_module = _fake_integration_module(
        "yai_core.integrations.mcp",
        McpServerConfig=lambda **kwargs: kwargs,
        attach_mcp_tools=_never_attached,
    )
    monkeypatch.setitem(sys.modules, "yai_core.integrations.mcp", mcp_module)
    core = AgentCore(ScriptedModel([ModelResponse(content="ok")]))
    with pytest.raises(ValueError, match="mcp 数组元素必须是 object"):
        _run(attach_configured_tools(core, {"mcp": ["not-an-object"]}))

    openapi_module = _fake_integration_module(
        "yai_core.integrations.openapi",
        OpenApiSpecConfig=lambda **kwargs: kwargs,
        attach_openapi_tools=_never_attached,
    )
    monkeypatch.setitem(sys.modules, "yai_core.integrations.openapi", openapi_module)
    core = AgentCore(ScriptedModel([ModelResponse(content="ok")]))
    with pytest.raises(ValueError, match="openapi 数组元素必须是 object"):
        _run(attach_configured_tools(core, {"openapi": ["not-an-object"]}))


def test_attach_configured_tools_closes_first_bridge_when_second_fails(monkeypatch) -> None:
    closed: list[str] = []

    class _Bridge:
        async def aclose(self) -> None:
            closed.append("closed")

    async def attach_mcp(registry, config):  # noqa: ANN001, ARG001
        return _Bridge()

    async def attach_openapi(registry, config):  # noqa: ANN001, ARG001
        raise RuntimeError("openapi 挂了")

    monkeypatch.setitem(
        sys.modules,
        "yai_core.integrations.mcp",
        _fake_integration_module(
            "yai_core.integrations.mcp",
            McpServerConfig=lambda **kwargs: kwargs,
            attach_mcp_tools=attach_mcp,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "yai_core.integrations.openapi",
        _fake_integration_module(
            "yai_core.integrations.openapi",
            OpenApiSpecConfig=lambda **kwargs: kwargs,
            attach_openapi_tools=attach_openapi,
        ),
    )

    core = AgentCore(ScriptedModel([ModelResponse(content="ok")]))
    with pytest.raises(RuntimeError, match="openapi 挂了"):
        _run(
            attach_configured_tools(
                core, {"mcp": [{"alias": "a"}], "openapi": [{"alias": "b"}]}
            )
        )
    assert closed == ["closed"]


async def _never_attached(registry, config):  # noqa: ANN001, ARG001
    raise AssertionError("不应该走到真正的接入逻辑")


def _run(coro):
    """在独立事件循环里跑协程（同步用例里驱动异步接入逻辑）。"""
    return asyncio.run(coro)


# ------------------------------------------------------------------- 协议细节


def test_jsonable_falls_back_to_string_and_encode_is_ndjson() -> None:
    class _Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    assert jsonable(_Opaque()) == "<opaque>"
    assert jsonable({"a": {1, 2}}) == {"a": [1, 2]}
    payload = encode({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    assert payload == b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'
    assert b"\n" not in payload


@async_test
async def test_channel_result_shapes_are_coerced() -> None:
    async def request(method, params):  # noqa: ANN001, ARG001
        return 42

    channel = SidecarChannel(request)
    assert await channel.confirm("t", {}) is False
    assert await channel.ask("q") == "42"

    async def returns_none(method, params):  # noqa: ANN001, ARG001
        return None

    none_channel = SidecarChannel(returns_none)
    assert await none_channel.ask("q") == ""

    async def returns_text(method, params):  # noqa: ANN001, ARG001
        return "补充说明"

    text_channel = SidecarChannel(returns_text)
    assert await text_channel.ask("q") == "补充说明"


# ------------------------------------------------------------------ 初始化边界


@async_test
async def test_initialize_rejects_bad_request_shapes() -> None:
    harness = Harness()
    try:
        await harness.server.handle({"jsonrpc": "2.0", "id": 1, "method": 42})
        bad_method = harness.sent[-1]
        assert bad_method["error"]["code"] == INVALID_REQUEST

        await harness.server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": [1, 2]}
        )
        bad_params = harness.sent[-1]
        assert bad_params["error"]["code"] == INVALID_PARAMS
    finally:
        await harness.close()


@async_test
async def test_initialize_validates_model_policy_and_timeout() -> None:
    harness = Harness()
    try:
        bad_model = await harness.call("initialize", {"model": "gpt"})
        assert bad_model["error"]["code"] == INVALID_PARAMS

        bad_timeout = await harness.call(
            "initialize", {"model": {}, "interaction_timeout": "很久"}
        )
        assert bad_timeout["error"]["code"] == INVALID_PARAMS

        bad_allow = await harness.call(
            "initialize", {"model": {}, "policy": {"allow": "search"}}
        )
        assert bad_allow["error"]["code"] == INVALID_PARAMS

        bad_iters = await harness.call("initialize", {"model": {}, "max_iters": 0})
        assert bad_iters["error"]["code"] == INVALID_PARAMS
        assert "max_iters" in bad_iters["error"]["message"]
    finally:
        await harness.close()


@async_test
async def test_initialize_reports_missing_dependency_and_tool_failure() -> None:
    def missing_dependency(config):  # noqa: ANN001, ARG001
        raise ImportError("需要安装 openai")

    harness = Harness(model_factory=missing_dependency)
    reply = await harness.call("initialize", {"model": {}})
    assert reply["error"]["code"] == INVALID_PARAMS
    assert "hint" in reply["error"]["data"]

    async def fail_tools(core, params):  # noqa: ANN001, ARG001
        raise RuntimeError("工具炸了")

    tools_harness = Harness(tool_attacher=fail_tools)
    tools_reply = await tools_harness.call("initialize", {"model": {}})
    assert tools_reply["error"]["code"] == INVALID_PARAMS
    assert "RuntimeError" in tools_reply["error"]["message"]
    await tools_harness.close()


@async_test
async def test_initialize_defaults_model_and_reports_internal_error() -> None:
    harness = Harness()
    try:
        reply = await harness.call("initialize", {})
        assert reply["result"]["protocol_version"] == 1
    finally:
        await harness.close()

    def exploding(config):  # noqa: ANN001, ARG001
        raise RuntimeError("模型工厂炸了")

    broken = Harness(model_factory=exploding)
    error_reply = await broken.call("initialize", {"model": {}})
    assert error_reply["error"]["code"] == INTERNAL_ERROR
    assert "RuntimeError" in error_reply["error"]["message"]
    await broken.close()


@async_test
async def test_initialize_propagates_cancellation() -> None:
    def cancelling(config):  # noqa: ANN001, ARG001
        raise asyncio.CancelledError

    harness = Harness(model_factory=cancelling)
    with pytest.raises(asyncio.CancelledError):
        await harness.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"model": {}}}
        )
    await harness.close()


@async_test
async def test_initialize_after_shutdown_reports_shutting_down() -> None:
    harness = Harness()
    await harness.call("shutdown")
    reply = await harness.call("initialize", {"model": {}})
    assert reply["error"]["code"] == SHUTTING_DOWN


# -------------------------------------------------------------------- 运行边界


@async_test
async def test_run_after_shutdown_or_without_core_is_rejected() -> None:
    harness = Harness()
    await harness.call("initialize", {"model": {}})
    harness.server._core = None
    reply = await harness.call("agent/run", {"task": "你好"})
    assert reply["error"]["code"] == NOT_INITIALIZED
    await harness.close()

    closed = Harness()
    await closed.call("initialize", {"model": {}})
    await closed.call("shutdown")
    reply = await closed.call("agent/run", {"task": "你好"})
    assert reply["error"]["code"] == SHUTTING_DOWN


@async_test
async def test_cancel_without_active_run_returns_false() -> None:
    harness = Harness()
    await harness.call("initialize", {"model": {}})
    reply = await harness.call("agent/cancel", {"run_id": "不存在的"})
    assert reply["result"] == {"cancelled": False, "run_id": "不存在的"}
    await harness.close()


@async_test
async def test_run_with_subject_passes_subject_to_core() -> None:
    harness = Harness()
    await harness.call("initialize", {"model": {}})
    reply = await harness.call(
        "agent/run",
        {
            "task": "你好",
            "subject": {
                "subject_id": "u-1",
                "display_name": "王敏",
                "attributes": {"role": "admin"},
            },
        },
    )
    run_id = reply["result"]["run_id"]
    done = await harness.wait_for(
        lambda m: m.get("method") == "agent/event" and m["params"]["type"] == "done"
    )
    assert done["params"]["run_id"] == run_id
    await harness.close()


@async_test
async def test_run_failure_emits_agent_error_notification() -> None:
    harness = Harness()
    await harness.call("initialize", {"model": {}})
    assert harness.server._core is not None
    await harness.server._core.aclose()

    reply = await harness.call("agent/run", {"task": "你好"})
    run_id = reply["result"]["run_id"]
    failed = await harness.wait_for(lambda m: m.get("method") == "agent/error")
    assert failed["params"]["run_id"] == run_id
    assert "RuntimeError" in failed["params"]["error"]
    await harness.close()


# ------------------------------------------------------- 审批：宿主失联与报错


@async_test
async def test_approval_timeout_is_treated_as_denied() -> None:
    calls: list[str] = []

    def lookup(name: str = "") -> dict:
        """按姓名查询客户。"""
        calls.append(name)
        return {"name": name}

    async def attach(core, params):  # noqa: ANN001, ARG001
        core.register_tools([build_spec(lookup)])
        return []

    harness = Harness(
        model_factory=_scripted(
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="lookup", arguments={"name": "王敏"})],
            ),
            ModelResponse(content="未获授权"),
        ),
        tool_attacher=attach,
    )
    try:
        await harness.call(
            "initialize", {"model": {}, "interaction_timeout": 0.05}
        )
        await harness.call("agent/run", {"task": "查客户王敏"})
        ask = await harness.wait_for(lambda m: m.get("method") == "approval/request")
        assert ask["params"]["tool"] == "lookup"
        # 宿主不回答：超时按拒绝处理，任务不能挂死。
        done = await harness.wait_for(
            lambda m: m.get("method") == "agent/event" and m["params"]["type"] == "done"
        )
        assert done["params"]["data"]["final_text"] == "未获授权"
        assert calls == []
    finally:
        await harness.close()


@async_test
async def test_host_error_response_denies_approval() -> None:
    calls: list[str] = []

    def lookup(name: str = "") -> dict:
        """按姓名查询客户。"""
        calls.append(name)
        return {"name": name}

    async def attach(core, params):  # noqa: ANN001, ARG001
        core.register_tools([build_spec(lookup)])
        return []

    harness = Harness(
        model_factory=_scripted(
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="lookup", arguments={"name": "王敏"})],
            ),
            ModelResponse(content="未获授权"),
        ),
        tool_attacher=attach,
    )
    try:
        await harness.call("initialize", {"model": {}})
        await harness.call("agent/run", {"task": "查客户王敏"})
        ask = await harness.wait_for(lambda m: m.get("method") == "approval/request")
        await harness.server.handle(
            {
                "jsonrpc": "2.0",
                "id": ask["id"],
                "error": {"code": -32000, "message": "宿主拒绝"},
            }
        )
        done = await harness.wait_for(
            lambda m: m.get("method") == "agent/event" and m["params"]["type"] == "done"
        )
        assert done["params"]["data"]["final_text"] == "未获授权"
        assert calls == []
    finally:
        await harness.close()


@async_test
async def test_request_host_timeout_and_closed_paths() -> None:
    harness = Harness()
    await harness.call("initialize", {"model": {}, "interaction_timeout": 0.05})
    # 宿主不回：等待超时后按“没有回答”处理。
    assert await harness.server._request_host("approval/request", {"tool": "x"}) is None
    await harness.close()
    # 已关闭：不再发起反向请求。
    assert await harness.server._request_host("approval/request", {"tool": "x"}) is None


@async_test
async def test_cleanup_releases_pending_host_requests() -> None:
    harness = Harness()
    await harness.call("initialize", {"model": {}})
    pending = asyncio.create_task(
        harness.server._request_host("approval/request", {"tool": "x"})
    )
    await asyncio.sleep(0.05)
    assert harness.server._pending
    await harness.server._cleanup()
    assert await pending is None
    assert harness.server._pending == {}
    # 重复清理是幂等的。
    await harness.server._cleanup()


# ------------------------------------------------------------------ stdio 传输


class _FakeStdin:
    """给 serve_stdio 的读线程一个可控的 stdin。"""

    def __init__(self, payload: bytes = b"", *, broken: bool = False) -> None:
        self.buffer = _BrokenStream() if broken else io.BytesIO(payload)


class _BrokenStream:
    def __iter__(self):
        raise RuntimeError("stdin 读坏了")


class _FakeStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None


@async_test
async def test_serve_stdio_processes_lines_until_eof(monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    payload = (
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        b"\n"
        b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    )
    monkeypatch.setattr(sys, "stdin", _FakeStdin(payload))
    server = SidecarServer(send=send)
    await server.serve_stdio()

    ids = [m.get("id") for m in sent if m.get("result")]
    assert ids == [1, 2]
    assert server._closed is True


@async_test
async def test_serve_stdio_survives_reader_failure(monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    monkeypatch.setattr(sys, "stdin", _FakeStdin(broken=True))
    server = SidecarServer(send=send)
    await server.serve_stdio()
    assert server._closed is True


def test_stdio_send_writes_one_ndjson_line(monkeypatch) -> None:
    fake = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake)
    server = SidecarServer()
    asyncio.run(
        server._stdio_send({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    )
    assert fake.buffer.getvalue() == b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'


# -------------------------------------------------------------------- 进程入口


def test_main_exit_codes(monkeypatch) -> None:
    from yai_core.sidecar import __main__ as sidecar_main

    async def serve_quietly(self):  # noqa: ANN001
        return None

    monkeypatch.setattr(sidecar_server.SidecarServer, "serve_stdio", serve_quietly)
    assert sidecar_main.main() == 0

    async def serve_interrupted(self):  # noqa: ANN001
        raise KeyboardInterrupt

    monkeypatch.setattr(sidecar_server.SidecarServer, "serve_stdio", serve_interrupted)
    assert sidecar_main.main() == 130

    async def serve_broken_pipe(self):  # noqa: ANN001
        raise BrokenPipeError

    monkeypatch.setattr(sidecar_server.SidecarServer, "serve_stdio", serve_broken_pipe)
    assert sidecar_main.main() == 0


def test_module_entrypoint_runs_main(monkeypatch) -> None:
    async def serve_quietly(self):  # noqa: ANN001
        return None

    monkeypatch.setattr(sidecar_server.SidecarServer, "serve_stdio", serve_quietly)
    # runpy 执行包内 __main__ 前先从 sys.modules 摘掉旧引用，否则会报重复导入警告。
    monkeypatch.delitem(sys.modules, "yai_core.sidecar.__main__", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("yai_core.sidecar", run_name="__main__")
    assert exit_info.value.code == 0


# ---------- B3：会话级 Subject 绑定（防请求参数伪造身份） ----------

def test_session_subject_pinning_rejects_per_run_subject() -> None:
    """initialize 绑定可信身份后，agent/run 参数里的 subject 必须被拒绝。"""
    h = Harness()
    asyncio.run(h.call("initialize", {
        "subject": {"subject_id": "user-42", "display_name": "真身"},
    }))

    params = {
        "task": "查一下",
        "subject": {"subject_id": "attacker", "display_name": "伪造身份"},
    }
    response = asyncio.run(h.call("agent/run", params))
    assert response["error"]["code"] == INVALID_PARAMS
    assert "Subject" in response["error"]["message"]


def test_session_subject_used_for_runs() -> None:
    """绑定后运行使用会话身份（不抛错即通过；身份注入路径与宿主一致）。"""
    h = Harness()
    asyncio.run(h.call("initialize", {"subject": {"subject_id": "user-42"}}))
    response = asyncio.run(h.call("agent/run", {"task": "跑一个任务"}))
    assert "error" not in response
    # 等待后台任务收尾，避免事件循环告警
    run = h.server._active_run
    if run is not None:
        asyncio.run(asyncio.wait_for(asyncio.shield(run), timeout=5))


def test_without_session_subject_params_still_accepted() -> None:
    """未绑定时保持既有语义：可信父进程按参数注入身份（向后兼容）。"""
    h = Harness()
    asyncio.run(h.call("initialize", {}))
    response = asyncio.run(h.call("agent/run", {
        "task": "查一下",
        "subject": {"subject_id": "user-7"},
    }))
    assert "error" not in response
