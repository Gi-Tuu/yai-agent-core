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
    TRUNCATED_FINAL_TEXT_MAX,
    BridgeConfig,
    BridgeError,
    BridgeOriginError,
    NativeBridge,
    build_truncated_message,
    default_config_path,
    encode_frame,
    load_bridge_config,
    read_frame,
    resolve_config_path,
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


def test_resolve_config_path_ignores_chrome_flags() -> None:
    """Chrome / Edge 在 Windows 会追加 --parent-window=0，不能当配置路径。"""
    assert (
        resolve_config_path(["chrome-extension://abc/", "--parent-window=0"], None)
        == default_config_path()
    )
    assert (
        resolve_config_path(["chrome-extension://abc/", r"C:\cfg\bridge.json"], None)
        == r"C:\cfg\bridge.json"
    )
    assert (
        resolve_config_path(["chrome-extension://abc/"], r"E:\env\bridge.json")
        == r"E:\env\bridge.json"
    )


def test_configure_llm_writes_local_config_and_masks_key(tmp_path) -> None:
    path = tmp_path / "bridge.json"
    path.write_text(
        json.dumps(
            {
                "allowed_origins": ["chrome-extension://abcdef/"],
                "initialize": {
                    "model": {"provider": "openai-compatible", "api_key": "sk-old"}
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_bridge_config(str(path))
    written: list[dict[str, Any]] = []

    async def writer(message: dict[str, Any]) -> None:
        written.append(message)

    async def reader() -> dict[str, Any] | None:
        return None

    bridge = NativeBridge(
        FakeTransport(),
        config=config,
        read_message=reader,
        write_message=writer,
        config_path=str(path),
    )
    bridge._hello_done = True  # noqa: SLF001 - 单测直接置就绪态，避免走完整握手
    _run(
        bridge._handle_browser_message(  # noqa: SLF001
            {
                "op": "configure_llm",
                "base_url": "https://api.new/v1",
                "model": "m1",
                "strong_model": "m2",
                "api_key": "sk-new-9999",
            }
        )
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    model = saved["initialize"]["model"]
    assert model["base_url"] == "https://api.new/v1"
    assert model["model"] == "m1"
    assert model["strong_model"] == "m2"
    assert model["api_key"] == "sk-new-9999"
    assert (tmp_path / "bridge.json.bak").is_file()

    assert written[-1]["type"] == "config_saved"
    assert written[-1]["model"]["api_key"] == "***9999"
    assert "sk-new-9999" not in json.dumps(written, ensure_ascii=False)


def test_configure_llm_without_config_path_is_rejected() -> None:
    written: list[dict[str, Any]] = []

    async def writer(message: dict[str, Any]) -> None:
        written.append(message)

    async def reader() -> dict[str, Any] | None:
        return None

    bridge = NativeBridge(
        FakeTransport(),
        config=BridgeConfig(allowed_origins=frozenset({"chrome-extension://abc"})),
        read_message=reader,
        write_message=writer,
    )
    bridge._hello_done = True  # noqa: SLF001
    _run(
        bridge._handle_browser_message(  # noqa: SLF001
            {"op": "configure_llm", "model": "m1"}
        )
    )
    assert written[-1]["type"] == "error"
    assert "配置文件" in written[-1]["message"]


def test_get_config_returns_masked_model(tmp_path) -> None:
    path = tmp_path / "bridge.json"
    path.write_text(
        json.dumps(
            {
                "allowed_origins": ["chrome-extension://abcdef/"],
                "initialize": {
                    "model": {
                        "provider": "openai-compatible",
                        "api_key": "sk-secret-1234",
                        "model": "m1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    written: list[dict[str, Any]] = []

    async def writer(message: dict[str, Any]) -> None:
        written.append(message)

    async def reader() -> dict[str, Any] | None:
        return None

    bridge = NativeBridge(
        FakeTransport(),
        config=load_bridge_config(str(path)),
        read_message=reader,
        write_message=writer,
        config_path=str(path),
    )
    bridge._hello_done = True  # noqa: SLF001
    _run(bridge._handle_browser_message({"op": "get_config"}))  # noqa: SLF001
    assert written[-1]["type"] == "llm_config"
    assert written[-1]["model"]["api_key"] == "***1234"
    assert written[-1]["model"]["model"] == "m1"


def test_load_config_validates(tmp_path) -> None:
    path = tmp_path / "bridge.json"
    path.write_text(
        json.dumps(
            {
                "allowed_origins": ["chrome-extension://abcdef/"],
                "initialize": {"policy": {"mode": "auto"}},
                "max_task_chars": 100,
                "sidecar_command": ["python", "-m", "yai_core.sidecar"],
            }
        ),
        encoding="utf-8",
    )
    config = load_bridge_config(str(path))
    assert config.allowed_origins == frozenset({"chrome-extension://abcdef"})
    assert config.max_task_chars == 100
    assert config.sidecar_command == ("python", "-m", "yai_core.sidecar")

    path.write_text(json.dumps({"max_task_chars": -1}), encoding="utf-8")
    with pytest.raises(BridgeError):
        load_bridge_config(str(path))

    path.write_text(
        json.dumps({"sidecar_command": "python -m yai_core.sidecar"}),
        encoding="utf-8",
    )
    with pytest.raises(BridgeError):
        load_bridge_config(str(path))


# ---------- 测试驱动 ----------


class FakeTransport:
    """假 Sidecar：记录请求，按方法自动回响，可注入通知 / 反向请求。"""

    def __init__(
        self, *, protocol_version: int = 1, auto_done_on_run: bool = False
    ) -> None:
        self.protocol_version = protocol_version
        self.auto_done_on_run = auto_done_on_run
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
            run_id = f"r{self.run_counter}"
            self.inbox.put_nowait(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"run_id": run_id},
                }
            )
            if self.auto_done_on_run:
                # 同一 burst 里紧跟终态：模拟"短任务 done 早于 run_started"。
                self.inbox.put_nowait(
                    {
                        "jsonrpc": "2.0",
                        "method": "agent/event",
                        "params": {
                            "run_id": run_id,
                            "seq": 0,
                            "type": "done",
                            "data": {"final_text": "已完成"},
                        },
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

    def __init__(
        self,
        config: BridgeConfig | None = None,
        *,
        yielding_writer: bool = False,
        **transport_kw: Any,
    ) -> None:
        self.yielding_writer = yielding_writer
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
        if self.yielding_writer:
            # 生产写路径是 asyncio.to_thread（必然让出），这里显式模拟。
            await asyncio.sleep(0)

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
    # Sidecar 退出后 run 必须报"未运行"，而不是拿残留 _run_id 误报 run_busy。
    assert any(item["code"] == "sidecar_not_running" for item in harness.of_type("error"))


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


# ---------- v0.26 修复回归：顺序 / 崩溃 / 超时 / 配置安全 ----------


@pytest.mark.parametrize("yielding_writer", [False, True])
def test_short_run_finished_before_run_started_is_not_restarted(
    yielding_writer: bool,
) -> None:
    """NB-1：终态先于 run_started 到达时，桥不补发 run_started、运行位保持空闲。

    ``yielding_writer=True`` 复刻生产写路径（asyncio.to_thread 必然让出），
    确保竞态修复不依赖测试用的"同步写"。
    """

    async def scenario() -> Harness:
        harness = Harness(
            auto_done_on_run=True, yielding_writer=yielding_writer
        )
        task = asyncio.create_task(harness.bridge.run())
        await _wait_for(harness, "bridge_ready")
        harness.send(_hello())
        await _wait_for(harness, "hello_ok")
        harness.send({"op": "run", "task": "短任务"})
        await asyncio.sleep(0.05)
        harness.send({"op": "status"})
        await _wait_for(harness, "status")
        harness.send({"op": "run", "task": "第二个任务"})
        await asyncio.sleep(0.05)
        harness.send(None)
        await task
        return harness

    harness = _run(scenario())
    notifications = harness.of_type("notification")
    assert notifications and notifications[0]["params"]["type"] == "done"
    # 迟到的 run_started 不应再发给扩展（否则面板会卡回"运行中"）。
    assert harness.of_type("run_started") == []
    assert harness.of_type("status")[-1]["active_run_id"] is None
    assert not any(item["code"] == "run_busy" for item in harness.of_type("error"))
    runs = [item for item in harness.transport.sent if item.get("method") == "agent/run"]
    assert len(runs) == 2  # 第二个 run 没被残留运行位挡住


def test_sidecar_exit_during_run_clears_slot_and_reports_not_running() -> None:
    """NB-2：运行中 Sidecar 退出后，运行位必须清空，后续 run 报 not_running。"""

    async def scenario() -> Harness:
        harness = Harness()
        task = asyncio.create_task(harness.bridge.run())
        await _wait_for(harness, "bridge_ready")
        harness.send(_hello())
        await _wait_for(harness, "hello_ok")
        harness.send({"op": "run", "task": "长任务"})
        await _wait_for(harness, "run_started")
        harness.transport.inbox.put_nowait(None)  # Sidecar 崩溃
        await _wait_for(harness, "sidecar_exit")
        harness.send({"op": "status"})
        await _wait_for(harness, "status")
        harness.send({"op": "run", "task": "第二个任务"})
        await _wait_for(harness, "error")
        harness.send(None)
        await task
        return harness

    harness = _run(scenario())
    status = harness.of_type("status")[-1]
    assert status["sidecar_alive"] is False
    assert status["active_run_id"] is None  # 不再残留 r1
    assert harness.of_type("error")[-1]["code"] == "sidecar_not_running"


def test_rpc_timeout_raises_and_orphan_response_is_dropped() -> None:
    """NB-3：RPC 超时转 BridgeError；迟到响应按孤儿丢弃，不冒充 notification。"""

    async def scenario() -> Harness:
        harness = Harness()
        task = asyncio.create_task(harness.bridge.run())
        await _wait_for(harness, "bridge_ready")
        harness.send(_hello())
        await _wait_for(harness, "hello_ok")
        with pytest.raises(BridgeError) as excinfo:
            await harness.bridge._rpc("ping", {}, timeout=0.01)  # noqa: SLF001
        assert excinfo.value.code == "sidecar_timeout"
        request_id = harness.transport.sent[-1]["id"]
        harness.transport.inbox.put_nowait(
            {"jsonrpc": "2.0", "id": request_id, "result": {"pong": True}}
        )
        await asyncio.sleep(0.02)
        harness.send(None)
        await task
        return harness

    harness = _run(scenario())
    orphans = [
        item
        for item in harness.outbox
        if item.get("type") == "notification" and item.get("method") is None
    ]
    assert orphans == []


def test_start_oserror_becomes_visible_error_frame() -> None:
    """启动失败（解释器不存在 / 无权限）必须回错误帧，而不是裸异常退出。"""

    class _BadStartTransport(FakeTransport):
        async def start(self) -> None:  # type: ignore[override]
            raise FileNotFoundError("sidecar_command 指向不存在的解释器")

    async def scenario() -> Harness:
        harness = Harness()
        harness.bridge.transport = _BadStartTransport()
        harness.send(None)
        await harness.bridge.run()
        return harness

    harness = _run(scenario())
    errors = harness.of_type("error")
    assert errors and errors[0]["code"] == "sidecar_start_failed"
    assert "无法启动 Sidecar" in errors[0]["message"]


def _bridge_with_config(
    tmp_path: Any, model: dict[str, Any]
) -> tuple[NativeBridge, list[dict[str, Any]]]:
    config_path = tmp_path / "bridge.json"
    config_path.write_text(
        json.dumps(
            {
                "allowed_origins": ["chrome-extension://abcdef/"],
                "initialize": {"model": model},
            }
        ),
        encoding="utf-8",
    )
    written: list[dict[str, Any]] = []

    async def writer(message: dict[str, Any]) -> None:
        written.append(message)

    async def reader() -> dict[str, Any] | None:
        return None

    bridge = NativeBridge(
        FakeTransport(),
        config=load_bridge_config(str(config_path)),
        read_message=reader,
        write_message=writer,
        config_path=str(config_path),
    )
    bridge._hello_done = True  # noqa: SLF001
    return bridge, written


def test_configure_llm_base_url_change_requires_new_api_key(tmp_path: Any) -> None:
    """CFG-1：改 base_url 必须带新 Key；只允许 http/https；旧 Key 不得被改道。"""
    bridge, written = _bridge_with_config(
        tmp_path,
        {
            "provider": "openai-compatible",
            "api_key": "sk-OLD-KEY-1111",
            "base_url": "https://api.trusted/v1",
        },
    )
    config_path = tmp_path / "bridge.json"

    _run(bridge._handle_browser_message({"op": "configure_llm", "base_url": "https://attacker.example/v1"}))  # noqa: SLF001
    assert written[-1]["type"] == "error"
    assert written[-1]["code"] == "api_key_required"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["initialize"]["model"]["base_url"] == "https://api.trusted/v1"
    assert saved["initialize"]["model"]["api_key"] == "sk-OLD-KEY-1111"

    _run(
        bridge._handle_browser_message(  # noqa: SLF001
            {
                "op": "configure_llm",
                "base_url": "file:///C:/secrets.json",
                "api_key": "sk-NEW-2222",
            }
        )
    )
    assert written[-1]["code"] == "invalid_base_url"

    _run(
        bridge._handle_browser_message(  # noqa: SLF001
            {
                "op": "configure_llm",
                "base_url": "https://api.new/v1",
                "api_key": "sk-NEW-2222",
            }
        )
    )
    assert written[-1]["type"] == "config_saved"
    assert written[-1]["model"]["api_key"] == "***2222"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["initialize"]["model"]["base_url"] == "https://api.new/v1"
    assert saved["initialize"]["model"]["api_key"] == "sk-NEW-2222"
    assert "sk-NEW-2222" not in json.dumps(written, ensure_ascii=False)


def test_configure_llm_write_is_atomic_with_backup(tmp_path: Any) -> None:
    """CFG-3：原子写 + 旧版本轮为 .bak + 不残留 temp/lock 文件。"""
    bridge, _written = _bridge_with_config(
        tmp_path,
        {"provider": "openai-compatible", "api_key": "sk-OLD", "model": "old"},
    )
    config_path = tmp_path / "bridge.json"
    original = config_path.read_text(encoding="utf-8")

    _run(bridge._handle_browser_message({"op": "configure_llm", "model": "new-model"}))  # noqa: SLF001

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["initialize"]["model"]["model"] == "new-model"
    assert saved["initialize"]["model"]["api_key"] == "sk-OLD"
    backup = tmp_path / "bridge.json.bak"
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8")) == json.loads(original)
    assert list(tmp_path.glob("*.tmp-*")) == []
    lock = tmp_path / "bridge.json.lock"
    assert lock.is_file()
    assert b"sk-" not in lock.read_bytes()


# ---------- VER-2 followup：超帧截断保留终态标识 ----------


def test_truncated_message_keeps_terminal_metadata_and_drops_evidence() -> None:
    """截断帧必须带 type/original_bytes/params.type/run_id，且不透传 evidence/verification。"""
    huge = {
        "type": "notification",
        "method": "agent/event",
        "params": {
            "run_id": "r1",
            "seq": 3,
            "type": "done",
            "data": {
                "final_text": "x" * 5000,
                "verification": {
                    "status": "verified_success",
                    "evidence": [{"kind": "page", "summary": "y" * 5000}],
                },
                "evidence": [{"kind": "page", "summary": "y" * 5000}],
            },
        },
    }

    frame = build_truncated_message(huge, 9000)

    assert frame["type"] == "notification"
    assert frame["truncated"] is True
    assert frame["original_bytes"] == 9000
    assert frame["method"] == "agent/event"
    # 顶层镜像字段：扩展不解析嵌套 params 也能识别终态。
    assert frame["event_type"] == "done"
    assert frame["run_id"] == "r1"
    assert frame["params"]["type"] == "done"
    assert frame["params"]["run_id"] == "r1"
    assert frame["params"]["seq"] == 3
    # final_text 可截断，但不能把整段大字段带过去。
    assert len(frame["params"]["data"]["final_text"]) == TRUNCATED_FINAL_TEXT_MAX
    dumped = json.dumps(frame, ensure_ascii=False)
    assert "verification" not in dumped
    assert "evidence" not in dumped
    assert len(encode_frame(frame)) - 4 < 9000


def test_truncated_error_and_host_request_keep_actionable_fields() -> None:
    """error 的原因、审批的工具名要保留短副本；arguments 等大字段丢弃。"""
    error_frame = build_truncated_message(
        {
            "type": "notification",
            "method": "agent/error",
            "params": {"run_id": "r2", "error": "boom" * 500},
        },
        4000,
    )
    assert error_frame["method"] == "agent/error"
    assert error_frame["params"]["run_id"] == "r2"
    assert error_frame["params"]["error"].startswith("boom")
    assert len(error_frame["params"]["error"]) <= 300

    approval = build_truncated_message(
        {
            "type": "host_request",
            "request_id": "srv-9",
            "method": "approval/request",
            "params": {"tool": "submit", "arguments": {"x": "y" * 5000}},
        },
        5000,
    )
    assert approval["request_id"] == "srv-9"
    assert approval["method"] == "approval/request"
    assert approval["params"]["tool"] == "submit"
    assert "arguments" not in approval["params"]


def test_oversized_done_notification_is_truncated_but_still_terminal() -> None:
    """构造超大 done：输出截断帧仍含 params.type/run_id，且 Bridge 运行位已释放。"""

    async def scenario() -> tuple[Harness, dict[str, Any], dict[str, Any]]:
        harness = Harness(
            config=BridgeConfig(
                allowed_origins=frozenset({"chrome-extension://abcdef"}),
                max_message_bytes=600,
            )
        )
        task = asyncio.create_task(harness.bridge.run())
        await _wait_for(harness, "bridge_ready")
        harness.send(_hello())
        await _wait_for(harness, "hello_ok")
        harness.send({"op": "run", "task": "产生巨大结果的短任务"})
        await _wait_for(harness, "run_started")
        harness.transport.push_notification(
            "agent/event",
            {
                "run_id": "r1",
                "seq": 0,
                "type": "done",
                "data": {
                    "final_text": "x" * 5000,
                    "verification": {"status": "verified_success"},
                    "evidence": [{"kind": "page", "summary": "y" * 5000}],
                },
            },
        )
        truncated = await _wait_for(harness, "notification")
        harness.send({"op": "status"})
        status = await _wait_for(harness, "status")
        harness.send(None)
        await task
        return harness, truncated, status

    _harness, truncated, status = _run(scenario())
    assert truncated.get("truncated") is True
    assert truncated["event_type"] == "done"
    assert truncated["run_id"] == "r1"
    assert truncated["params"]["type"] == "done"
    assert truncated["params"]["run_id"] == "r1"
    assert "verification" not in json.dumps(truncated)
    assert "evidence" not in json.dumps(truncated)
    # 终态照样结算运行位（不会因截断而卡运行中）。
    assert status["active_run_id"] is None
    assert len(encode_frame(truncated)) - 4 <= 600


def test_oversized_done_compacts_within_configured_frame_limit() -> None:
    """极小的自定义上限触发二次瘦身：帧仍 <= 上限且保留 params.type/run_id。"""

    async def scenario() -> dict[str, Any]:
        harness = Harness(
            config=BridgeConfig(
                allowed_origins=frozenset({"chrome-extension://abcdef"}),
                max_message_bytes=300,
            )
        )
        task = asyncio.create_task(harness.bridge.run())
        await _wait_for(harness, "bridge_ready")
        harness.send(_hello())
        await _wait_for(harness, "hello_ok")
        harness.send({"op": "run", "task": "大结果"})
        await _wait_for(harness, "run_started")
        harness.transport.push_notification(
            "agent/event",
            {
                "run_id": "r1",
                "seq": 0,
                "type": "done",
                "data": {"final_text": "x" * 5000},
            },
        )
        truncated = await _wait_for(harness, "notification")
        harness.send(None)
        await task
        return truncated

    truncated = _run(scenario())
    assert truncated.get("truncated") is True
    assert truncated["params"]["type"] == "done"
    assert truncated["params"]["run_id"] == "r1"
    assert truncated["event_type"] == "done"
    assert len(encode_frame(truncated)) - 4 <= 300
