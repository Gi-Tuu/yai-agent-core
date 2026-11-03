"""Native Messaging Bridge 离线测试（v0.26 Batch 4）。

不启动 Chrome、不启动真实 Sidecar：用内存帧编解码 + 假 Sidecar 传输覆盖：
1. 帧编解码（4 字节小端长度、截断、超限、非法 JSON）；
2. 扩展来源校验（fail closed）；
3. hello 协议协商与版本不匹配；
4. run / cancel / status / shutdown 与 JSON-RPC 映射；
5. Sidecar 通知转发、运行位释放、sidecar 退出通知；
6. 反向请求（审批 / 澄清）与宿主应答回传；
7. 大小限制与出站截断、操作白名单、hello 前置。
"""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import pytest

from yai_core.sidecar.native_bridge import (
    BRIDGE_PROTOCOL_VERSION,
    MAX_MESSAGE_BYTES,
    BridgeConfig,
    BridgeError,
    BridgeOriginError,
    NativeBridge,
    encode_frame,
    load_bridge_config,
    read_frame,
    write_frame,
)


def _run(coro):
    return asyncio.run(coro)


# ---------- 帧编解码 ----------


def test_frame_roundtrip() -> None:
    buffer = io.BytesIO()
    write_frame(buffer, {"type": "hello", "v": 1})
    buffer.seek(0)
    assert read_frame(buffer) == {"type": "hello", "v": 1}
    assert read_frame(buffer) is None  # EOF


def test_frame_header_is_little_endian_length() -> None:
    frame = encode_frame({"a": 1})
    length = int.from_bytes(frame[:4], "little")
    assert length == len(frame) - 4


def test_frame_rejects_truncated_header() -> None:
    buffer = io.BytesIO(b"\x01\x00")
    with pytest.raises(BridgeError):
        read_frame(buffer)


def test_frame_rejects_oversize() -> None:
    buffer = io.BytesIO((MAX_MESSAGE_BYTES + 1).to_bytes(4, "little") + b"{}")
    with pytest.raises(BridgeError):
        read_frame(buffer)


def test_frame_rejects_invalid_json() -> None:
    payload = b"{not json"
    buffer = io.BytesIO(len(payload).to_bytes(4, "little") + payload)
    with pytest.raises(BridgeError):
        read_frame(buffer)


def test_frame_rejects_non_object() -> None:
    payload = b"[1,2]"
    buffer = io.BytesIO(len(payload).to_bytes(4, "little") + payload)
    with pytest.raises(BridgeError):
        read_frame(buffer)


# ---------- 来源校验 ----------


def test_origin_must_be_allowlisted() -> None:
    config = BridgeConfig(
        allowed_origins=frozenset({"chrome-extension://abcdef"}),
    )
    assert config.check_origin("chrome-extension://abcdef/") == "chrome-extension://abcdef"
    with pytest.raises(BridgeOriginError):
        config.check_origin("chrome-extension://other")
    with pytest.raises(BridgeOriginError):
        config.check_origin(None)
    with pytest.raises(BridgeOriginError):
        config.check_origin("https://evil.test")


def test_empty_allowlist_rejects_everything() -> None:
    with pytest.raises(BridgeOriginError):
        BridgeConfig().check_origin("chrome-extension://abcdef")


def test_load_config_validates(tmp_path) -> None:
    path = tmp_path / "bridge.json"
    path.write_text(
        json.dumps(
            {
                "allowed_origins": ["chrome-extension://abcdef/"],
                "initialize": {"policy": {"mode": "auto"}},
                "max_task_chars": 100,
            }
        ),
        encoding="utf-8",
    )
    config = load_bridge_config(str(path))
    assert config.allowed_origins == frozenset({"chrome-extension://abcdef"})
    assert config.max_task_chars == 100

    path.write_text(json.dumps({"max_task_chars": -1}), encoding="utf-8")
    with pytest.raises(BridgeError):
        load_bridge_config(str(path))


# ---------- 测试驱动 ----------


class FakeTransport:
    """假 Sidecar：记录请求，按方法自动回响，可注入通知 / 反向请求。"""

    def __init__(self, *, protocol_version: int = 1) -> None:
        self.protocol_version = protocol_version
        self.started = False
        self.closed = False
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.run_counter = 0

    async def start(self) -> None:
        self.started = True

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        method = message.get("method")
        if method == "initialize":
            self.inbox.put_nowait(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocol_version": self.protocol_version,
                        "kernel_version": "0.25.0",
                    },
                }
            )
        elif method == "agent/run":
            self.run_counter += 1
            self.inbox.put_nowait(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"run_id": f"r{self.run_counter}"},
                }
            )
        elif method == "agent/cancel":
            self.inbox.put_nowait(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"cancelled": True, "run_id": "r1"},
                }
            )
        elif method == "shutdown":
            self.inbox.put_nowait(
                {"jsonrpc": "2.0", "id": message["id"], "result": {"stopped": True}}
            )

    async def receive(self) -> dict[str, Any] | None:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True

    def push_notification(self, method: str, params: dict[str, Any]) -> None:
        self.inbox.put_nowait({"jsonrpc": "2.0", "method": method, "params": params})

    def push_request(self, request_id: str, method: str, params: dict[str, Any]) -> None:
        self.inbox.put_nowait(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )


class Harness:
    """浏览器侧：脚本化入站消息 + 记录出站消息。"""

    def __init__(self, config: BridgeConfig | None = None, **transport_kw: Any) -> None:
        self.transport = FakeTransport(**transport_kw)
        self.config = config or BridgeConfig(
            allowed_origins=frozenset({"chrome-extension://abcdef"}),
            initialize_params={"policy": {"mode": "auto"}},
        )
        self.inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.outbox: list[dict[str, Any]] = []
        self.bridge = NativeBridge(
            self.transport,
            config=self.config,
            read_message=self._read,
            write_message=self._write,
        )

    async def _read(self) -> dict[str, Any] | None:
        return await self.inbox.get()

    async def _write(self, message: dict[str, Any]) -> None:
        self.outbox.append(message)

    def send(self, message: dict[str, Any] | None) -> None:
        self.inbox.put_nowait(message)

    def types(self) -> list[str]:
        return [str(item.get("type")) for item in self.outbox]

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [item for item in self.outbox if item.get("type") == kind]


def _hello(op: str = "hello", **extra: Any) -> dict[str, Any]:
    message = {"op": "hello", "protocol": BRIDGE_PROTOCOL_VERSION}
    message.update(extra)
    return message


async def _wait_for(harness: Harness, kind: str, *, timeout: float = 1.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        items = harness.of_type(kind)
        if items:
            return items[-1]
        await asyncio.sleep(0.005)
    raise AssertionError(f"未等到出站消息 {kind}，已收到 {harness.types()}")


# ---------- 会话流程 ----------


def test_hello_and_run_flow() -> None:
    async def scenario() -> Harness:
        harness = Harness()
        harness.send(_hello())
        harness.send({"op": "run", "task": "整理当前页面"})
        harness.send({"op": "status"})
        harness.send({"op": "shutdown"})
        harness.send(None)
        await harness.bridge.run()
        return harness

    harness = _run(scenario())
    methods = [item.get("method") for item in harness.transport.sent]
    assert methods == ["initialize", "agent/run", "shutdown"]
    assert "bridge_ready" in harness.types()
    hello_ok = _run(_wait_for(harness, "hello_ok"))
    assert hello_ok["sidecar_protocol_version"] == 1
    started = harness.of_type("run_started")[0]
    assert started["run_id"] == "r1"
    status = harness.of_type("status")[0]
    assert status["active_run_id"] == "r1"
    assert status["sidecar_alive"] is True
    assert harness.transport.closed is True


def test_run_requires_hello_first() -> None:
    async def scenario() -> Harness:
        harness = Harness()
        harness.send({"op": "run", "task": "x"})
        harness.send({"op": "ping"})
        harness.send(None)
        await harness.bridge.run()
        return harness

    harness = _run(scenario())
    errors = harness.of_type("error")
    assert errors and "hello" in errors[0]["message"]
    assert not [item for item in harness.transport.sent if item.get("method") == "agent/run"]


def test_protocol_mismatch_is_rejected() -> None:
    async def scenario() -> Harness:
        harness = Harness()
        harness.send(_hello(protocol=99))
        harness.send({"op": "ping"})
        harness.send(None)
        await harness.bridge.run()
        return harness

    harness = _run(scenario())
    rejected = harness.of_type("hello_rejected")
    assert rejected and rejected[0]["expected"] == BRIDGE_PROTOCOL_VERSION
    assert rejected[0]["received"] == 99


def test_unknown_op_rejected() -> None:
    async def scenario() -> Harness:
        harness = Harness()
        harness.send({"op": "import-credentials", "api_key": "sk-x"})
        harness.send(None)
        await harness.bridge.run()
        return harness

    harness = _run(scenario())
    assert harness.of_type("error")[0]["code"] == "unsupported_op"
    # 浏览器消息里的凭据字段不会被转发给 Sidecar（只发生 initialize / 收尾 shutdown）。
    methods = [item.get("method") for item in harness.transport.sent]
    assert "agent/run" not in methods
    assert all(item.get("method") in ("initialize", "shutdown") for item in harness.transport.sent)


def test_task_length_limit_enforced() -> None:
    async def scenario() -> Harness:
        harness = Harness(
            config=BridgeConfig(
                allowed_origins=frozenset({"chrome-extension://abcdef"}),
                max_task_chars=5,
            )
        )
        harness.send(_hello())
        harness.send({"op": "run", "task": "超长任务文本"})
        harness.send(None)
        await harness.bridge.run()
        return harness

    harness = _run(scenario())
    assert any("过长" in item["message"] for item in harness.of_type("error"))
    assert not [item for item in harness.transport.sent if item.get("method") == "agent/run"]


def test_second_run_rejected_while_active() -> None:
    async def scenario() -> Harness:
        harness = Harness()
        harness.send(_hello())
        harness.send({"op": "run", "task": "任务一"})
        harness.send({"op": "run", "task": "任务二"})
        harness.send(None)
        await harness.bridge.run()
        return harness

    harness = _run(scenario())
    errors = harness.of_type("error")
    assert any(item["code"] == "run_busy" for item in errors)


def test_cancel_forwarded() -> None:
    async def scenario() -> Harness:
        harness = Harness()
        harness.send(_hello())
        harness.send({"op": "run", "task": "任务"})
        harness.send({"op": "cancel", "run_id": "r1"})
        harness.send(None)
        await harness.bridge.run()
        return harness

    harness = _run(scenario())
    cancel_calls = [item for item in harness.transport.sent if item.get("method") == "agent/cancel"]
    assert cancel_calls and cancel_calls[0]["params"] == {"run_id": "r1"}
    assert harness.of_type("run_cancel_requested")[0]["cancelled"] is True


def test_notifications_forwarded_and_run_slot_released() -> None:
    async def scenario() -> Harness:
        harness = Harness()
        task = asyncio.create_task(harness.bridge.run())
        await _wait_for(harness, "bridge_ready")
        harness.send(_hello())
        await _wait_for(harness, "hello_ok")
        harness.send({"op": "run", "task": "任务"})
        await _wait_for(harness, "run_started")
        harness.transport.push_notification(
            "agent/event", {"run_id": "r1", "seq": 0, "type": "done", "data": {}}
        )
        await asyncio.sleep(0.02)
        harness.send({"op": "status"})
        harness.send({"op": "run", "task": "任务二"})
        await _wait_for(harness, "status")
        harness.send(None)
        await task
        return harness

    harness = _run(scenario())
    notifications = [
        item for item in harness.outbox
        if item.get("type") == "notification" and item.get("method") == "agent/event"
    ]
    assert notifications and notifications[0]["params"]["type"] == "done"
    # done 之后运行位释放，第二次 run 可以开始（run_id 变成 r2）。
    started = harness.of_type("run_started")
    assert [item["run_id"] for item in started] == ["r1", "r2"]


def test_host_request_forward_and_response() -> None:
    async def scenario() -> Harness:
        harness = Harness()
        task = asyncio.create_task(harness.bridge.run())
        await _wait_for(harness, "bridge_ready")
        harness.send(_hello())
        await _wait_for(harness, "hello_ok")
        harness.send({"op": "run", "task": "提交表单"})
        await _wait_for(harness, "run_started")
        harness.transport.push_request(
            "srv-1", "approval/request", {"tool": "submit", "arguments": {}}
        )
        host_req = await _wait_for(harness, "host_request")
        harness.send(
            {
                "op": "host_response",
                "request_id": host_req["request_id"],
                "result": {"approved": True},
            }
        )
        await asyncio.sleep(0.02)
        harness.send(None)
        await task
        return harness

    harness = _run(scenario())
    host_req = harness.of_type("host_request")[0]
    assert host_req["method"] == "approval/request"
    assert host_req["request_id"] == "srv-1"
    responses = [item for item in harness.transport.sent if item.get("id") == "srv-1"]
    assert responses and responses[0]["result"] == {"approved": True}


def test_host_response_rejects_foreign_request_ids() -> None:
    async def scenario() -> Harness:
        harness = Harness()
        harness.send(_hello())
        harness.send(
            {"op": "host_response", "request_id": "evil-1", "result": {"approved": True}}
        )
        harness.send(None)
        await harness.bridge.run()
        return harness

    harness = _run(scenario())
    assert harness.of_type("error")


def test_sidecar_exit_is_visible_and_stops_runs() -> None:
    async def scenario() -> Harness:
        harness = Harness()
        task = asyncio.create_task(harness.bridge.run())
        await _wait_for(harness, "bridge_ready")
        harness.send(_hello())
        await _wait_for(harness, "hello_ok")
        harness.transport.inbox.put_nowait(None)  # 模拟 Sidecar 退出（EOF）
        await _wait_for(harness, "sidecar_exit")
        harness.send({"op": "run", "task": "任务"})
        await _wait_for(harness, "error")
        harness.send(None)
        await task
        return harness

    harness = _run(scenario())
    assert harness.of_type("sidecar_exit")
    assert any(item["code"] == "bridge_error" for item in harness.of_type("error"))


def test_sidecar_protocol_mismatch_fails_start() -> None:
    async def scenario() -> Harness:
        harness = Harness(protocol_version=2)
        task = asyncio.create_task(harness.bridge.run())
        await _wait_for(harness, "error")
        harness.send(None)
        await task
        return harness

    harness = _run(scenario())
    assert any("协议版本" in item["message"] for item in harness.of_type("error"))


def test_oversized_outbound_frame_is_truncated() -> None:
    async def scenario() -> Harness:
        harness = Harness(
            config=BridgeConfig(
                allowed_origins=frozenset({"chrome-extension://abcdef"}),
                max_message_bytes=200,
            )
        )
        task = asyncio.create_task(harness.bridge.run())
        await _wait_for(harness, "bridge_ready")
        harness.send(_hello())
        await _wait_for(harness, "hello_ok")
        harness.transport.push_notification(
            "agent/event", {"run_id": "r1", "data": {"blob": "x" * 5000}}
        )
        truncated = await _wait_for(harness, "notification")
        assert truncated.get("truncated") is True
        assert truncated["original_bytes"] > 200
        harness.send(None)
        await task
        return harness

    harness = _run(scenario())
    assert harness.of_type("notification")
