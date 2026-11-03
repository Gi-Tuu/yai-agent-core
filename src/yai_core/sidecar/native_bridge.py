"""Native Messaging Bridge：Chrome / Edge 扩展 ↔ YAI Sidecar 的协议转换。

为什么需要它：浏览器 Native Messaging 用 **4 字节小端长度前缀 + UTF-8 JSON**，
而 YAI Sidecar 的公开协议是 **stdio NDJSON + JSON-RPC 2.0**。两者语义并不冲突，
所以不改 Sidecar 协议，只加一层独立 Bridge 做封装转换（任务书 §5.3）。

安全模型（重要）：

- Bridge 是**本地进程**，由浏览器按 native host manifest 拉起；Chrome 会把
  扩展来源作为第一个参数传入（``chrome-extension://<id>/``）。Bridge 只在
  来源命中 ``allowed_origins`` 时启动，否则直接退出——不信任扩展自报身份。
- **浏览器端不保管任何高权限凭据**：模型 / 策略等 initialize 参数默认来自 Bridge
  自己的本地配置文件或环境变量；扩展只能在用户显式点击"保存"时经 `configure_llm`
  写本地配置（Key 只走一次内存、不落浏览器存储，且读回时永远脱敏）。
  **换 base_url / provider 必须同时提供新的 api_key**——旧 Key 绝不会被改道送到
  新端点；base_url 只接受 http/https。
- 扩展只能发白名单内的操作（hello / run / cancel / host_response / ping /
  status / shutdown），每个入站消息都有大小与字段校验。
- 出站事件超过 Native Messaging 单帧上限时替换为**可识别终态的截断帧**（保留
  `type`/`method`/`params.type`/`run_id`，final_text 可截断，evidence/verification
  等大字段一律丢弃），避免整帧被浏览器丢弃/杀死 host，也避免扩展永远等不到 done。
- 配置写盘是**原子**的（同目录 temp + fsync + os.replace，旧版本轮为 `.bak`），
  Bridge 与本地 CLI 共用同一把文件锁与同一写入口，避免半截 JSON / 丢更新。

生命周期诚实声明：Bridge 是与一次扩展连接绑定的短期进程；连接断开时 Sidecar
随之结束，**不提供跨进程任务恢复**。扩展重连后必须把结果视为 ``unknown``，
不得自动重做可能已有副作用的操作（见 ``docs/browser-extension-bridge.md``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import struct
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

#: Bridge 自身的协议版本（与 Sidecar 的 PROTOCOL_VERSION 分开演进）。
BRIDGE_PROTOCOL_VERSION = 1

#: Bridge → Sidecar 的 RPC 超时（秒）；initialize 可能拉 MCP / OpenAPI，单独放宽。
#: 超时转成可见的 ``sidecar_timeout`` 错误，不能让串行主循环无限等待。
RPC_TIMEOUT_DEFAULT = 10.0
RPC_TIMEOUTS: dict[str, float] = {
    "initialize": 60.0,
    "agent/run": 10.0,
    "agent/cancel": 10.0,
    "shutdown": 5.0,
}

#: Native Messaging 单帧上限：Chrome 对 host→browser 的消息有 1 MiB 限制。
MAX_MESSAGE_BYTES = 1_048_576
_FRAME_HEADER = struct.Struct("<I")

#: 扩展允许发送的操作白名单。
ALLOWED_OPS = frozenset(
    {
        "hello",
        "run",
        "cancel",
        "host_response",
        "ping",
        "status",
        "shutdown",
        "get_config",
        "configure_llm",
    }
)

#: Sidecar 允许反向请求宿主的白名单方法。
HOST_REQUEST_METHODS = frozenset({"approval/request", "clarify/request"})

ReadMessage = Callable[[], Awaitable[dict[str, Any] | None]]
WriteMessage = Callable[[dict[str, Any]], Awaitable[None]]


class BridgeError(Exception):
    """Bridge 层可安全回给扩展的错误。"""

    def __init__(self, message: str, *, code: str = "bridge_error") -> None:
        super().__init__(message)
        self.code = code


class BridgeOriginError(BridgeError):
    """扩展来源未授权（fail closed）。"""


class ConfigLockError(OSError):
    """配置写锁被占用/不可用：调用方必须放弃本次读改写，而不是无锁写入。"""


def _try_lock_fd(fd: int) -> None:
    """非阻塞获取 OS 文件锁；被占用时抛 ``OSError``。"""
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":  # pragma: no cover - Windows 专用分支
        msvcrt = importlib.import_module("msvcrt")
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl = importlib.import_module("fcntl")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    """释放 ``_try_lock_fd`` 取得的锁（尽力而为；关闭 fd 时 OS 也会兜底释放）。"""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":  # pragma: no cover - Windows 专用分支
            msvcrt = importlib.import_module("msvcrt")
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - 解锁失败没有补救手段
        pass


# ---------------------------------------------------------------- 帧编解码


def encode_frame(message: dict[str, Any]) -> bytes:
    """把 JSON 对象编码成 Native Messaging 帧（4 字节小端长度 + UTF-8）。"""
    payload = json.dumps(
        message, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")
    return _FRAME_HEADER.pack(len(payload)) + payload


def read_frame(stream: Any) -> dict[str, Any] | None:
    """从二进制流读一帧；EOF 返回 None。格式非法/超限抛 :class:`BridgeError`。"""
    header = stream.read(_FRAME_HEADER.size)
    if header is None or len(header) == 0:
        return None
    if len(header) != _FRAME_HEADER.size:
        raise BridgeError("帧头不完整（连接可能已断开）")
    (length,) = _FRAME_HEADER.unpack(header)
    if length <= 0:
        raise BridgeError("帧长度非法")
    if length > MAX_MESSAGE_BYTES:
        raise BridgeError(f"帧超过上限：{length} > {MAX_MESSAGE_BYTES}")
    payload = stream.read(length)
    if payload is None or len(payload) != length:
        raise BridgeError("帧体不完整（连接可能已断开）")
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError(f"帧不是合法 JSON：{exc}") from exc
    if not isinstance(message, dict):
        raise BridgeError("帧必须是 JSON object")
    return message


def write_frame(stream: Any, message: dict[str, Any]) -> None:
    stream.write(encode_frame(message))
    stream.flush()


# ---------------------------------------------------------------- 超帧截断
#
# Chrome 丢弃超限帧 / 杀 host，所以 Bridge 必须把超帧换成小帧；但"只留 type"
# 会让扩展永远收不到 done/cancelled/error，运行态卡死。截断帧因此保留：
# - 顶层：type / truncated / original_bytes / method / request_id（可审计）；
# - 通知：顶层 event_type / run_id，以及精简后的 params.type/run_id/seq/status/error/reason；
# - 只保留（可截断的）final_text，绝不透传 data.verification / data.evidence 等大字段。
# 扩展据此识别终态并清理运行态（见 extension/service_worker.js、sidepanel.js）。

#: 截断帧里 final_text 的上限（人看的摘要足够，不需要全文）。
TRUNCATED_FINAL_TEXT_MAX = 200
#: 截断帧里 run_id / type / method 等标识类字段的上限。
TRUNCATED_META_MAX = 120
#: 截断帧里 error / reason / question 等解释性字段的上限。
TRUNCATED_ERROR_MAX = 300


def _frame_payload_size(message: dict[str, Any]) -> int:
    return len(encode_frame(message)) - _FRAME_HEADER.size


def _truncated_scalar(
    value: Any, *, limit: int, allow_number: bool = False
) -> Any:
    """只保留短标量：str 截断、int/float/bool 原样；dict/list 一律丢弃。"""
    if isinstance(value, bool):
        return value if allow_number else None
    if isinstance(value, (int, float)):
        return value if allow_number else None
    if isinstance(value, str):
        return value[:limit]
    return None


def build_truncated_message(
    message: dict[str, Any],
    original_bytes: int,
    *,
    compact: bool = False,
) -> dict[str, Any]:
    """把超帧消息压成可识别终态的小帧（不包含 evidence / verification 等大字段）。

    Args:
        message: 原始出站消息（Bridge → 浏览器协议）。
        original_bytes: 原始 JSON 负载字节数，写进 ``original_bytes`` 便于排障。
        compact: 极端小上限时的二次瘦身：丢 ``data`` 与 tool/question，只留终态标识。
    """
    msg_type = message.get("type")
    replacement: dict[str, Any] = {
        "type": msg_type if isinstance(msg_type, str) else "notification",
        "truncated": True,
        "original_bytes": int(original_bytes),
    }
    request_id = message.get("request_id")
    if isinstance(request_id, str):
        replacement["request_id"] = request_id[:TRUNCATED_META_MAX]
    method = message.get("method")
    if isinstance(method, str):
        replacement["method"] = method[:TRUNCATED_META_MAX]
    # Bridge 自身的 error 帧：保留 code 与短 message。
    code = message.get("code")
    if isinstance(code, str):
        replacement["code"] = code[:TRUNCATED_META_MAX]
    text = message.get("message")
    if isinstance(text, str):
        replacement["message"] = text[:TRUNCATED_ERROR_MAX]

    params = message.get("params")
    if not isinstance(params, dict):
        return replacement

    slim: dict[str, Any] = {}
    for key in ("type", "run_id"):
        value = _truncated_scalar(params.get(key), limit=TRUNCATED_META_MAX)
        if value is not None:
            slim[key] = value
    seq = params.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        slim["seq"] = seq
    status = _truncated_scalar(params.get("status"), limit=TRUNCATED_META_MAX)
    if status is not None:
        slim["status"] = status
    for key in ("error", "reason"):
        value = _truncated_scalar(params.get(key), limit=TRUNCATED_ERROR_MAX)
        if value is not None:
            slim[key] = value
    if not compact:
        # host_request 需要最小的上下文（工具名 / 问题），但不透传 arguments/preview。
        for key in ("tool", "question"):
            value = _truncated_scalar(params.get(key), limit=TRUNCATED_ERROR_MAX)
            if value is not None:
                slim[key] = value
        data = params.get("data")
        if isinstance(data, dict):
            final_text = data.get("final_text")
            if isinstance(final_text, str) and final_text:
                slim["data"] = {"final_text": final_text[:TRUNCATED_FINAL_TEXT_MAX]}
    if slim:
        replacement["params"] = slim
        # 便于扩展在不解析嵌套 params 的情况下识别终态（任务书允许 event_type/run_id）。
        event_type = slim.get("type")
        if isinstance(event_type, str):
            replacement["event_type"] = event_type
        run_id = slim.get("run_id")
        if isinstance(run_id, str):
            replacement["run_id"] = run_id
    return replacement


# ---------------------------------------------------------------- Sidecar 传输


class SidecarTransport(Protocol):
    """Bridge 到 Sidecar 的最小传输契约（真实实现是子进程 stdio）。"""

    async def start(self) -> None: ...

    async def send(self, message: dict[str, Any]) -> None: ...

    async def receive(self) -> dict[str, Any] | None: ...

    async def close(self) -> None: ...


class SubprocessSidecarTransport:
    """用 ``python -m yai_core.sidecar`` 子进程承载 Sidecar（stdio NDJSON）。"""

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = list(command or [sys.executable, "-m", "yai_core.sidecar"])
        self.cwd = cwd
        self.env = env
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # stderr 继承：sidecar 的诊断只走 stderr，Bridge 不改写它。
            stderr=None,
        )

    def _require(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise BridgeError("Sidecar 尚未启动")
        return self._process

    async def send(self, message: dict[str, Any]) -> None:
        process = self._require()
        assert process.stdin is not None
        process.stdin.write(encode_ndjson(message))
        await process.stdin.drain()

    async def receive(self) -> dict[str, Any] | None:
        process = self._require()
        assert process.stdout is not None
        line = await process.stdout.readline()
        if not line:
            return None
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError(f"Sidecar 输出不是合法 JSON 行：{exc}") from exc
        if not isinstance(message, dict):
            raise BridgeError("Sidecar 输出必须是 JSON object")
        return message

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except Exception:  # noqa: BLE001 - 关闭路径尽力而为
                pass
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        # 显式关闭子进程 transport：Python 3.13 上若留给它随事件循环回收，
        # 解释器退出时会对已关闭的管道发 ResourceWarning（stderr 噪音）。
        transport = getattr(process, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(0)


def encode_ndjson(message: dict[str, Any]) -> bytes:
    return (
        json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)
        + "\n"
    ).encode("utf-8")


# ---------------------------------------------------------------- 配置


@dataclass(frozen=True)
class BridgeConfig:
    """Bridge 的本地可信配置（绝不来自浏览器消息）。

    Args:
        allowed_origins: 允许连接的扩展来源，如 ``chrome-extension://<id>/``；
            空集合 = 拒绝所有（fail closed）。
        initialize_params: 透传给 Sidecar ``initialize`` 的参数（model / policy /
            interaction_timeout 等）；扩展无权修改。
        max_task_chars: 单个任务文本上限（默认 4000）。
        max_message_bytes: 出站帧上限，超过则截断为标记（默认 1 MiB）。
        sidecar_command: 启动 Sidecar 的命令（如 ``["python", "-m", "yai_core.sidecar"]``）。
            空表示用当前解释器；**PyInstaller 冻结成 exe 时必须显式给**，否则
            ``sys.executable`` 会变成 Bridge 自身、把 Bridge 当 Sidecar 递归拉起。
    """

    allowed_origins: frozenset[str] = frozenset()
    initialize_params: dict[str, Any] = field(default_factory=dict)
    max_task_chars: int = 4000
    max_message_bytes: int = MAX_MESSAGE_BYTES
    sidecar_command: tuple[str, ...] = ()

    def check_origin(self, origin: str | None) -> str:
        """校验 Chrome 传入的扩展来源；不合法直接拒绝。"""
        normalized = _normalize_origin(origin)
        if not normalized:
            raise BridgeOriginError("缺少扩展来源参数（chrome-extension://<id>/）")
        if normalized not in self.allowed_origins:
            raise BridgeOriginError(f"扩展来源未授权：{normalized}")
        return normalized


def _normalize_origin(origin: str | None) -> str:
    if not origin:
        return ""
    text = origin.strip().rstrip("/")
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme != "chrome-extension" or not parts.netloc:
        return ""
    return f"chrome-extension://{parts.netloc}"


def mask_api_key(value: Any) -> str:
    """把 Key 脱敏成 ``***末4位``（空值 → 空串）。脚本与 Bridge 共用。"""
    key = str(value or "")
    return ("***" + key[-4:]) if key else ""


def masked_model(model: Any) -> dict[str, Any]:
    """返回可安全发给浏览器的模型配置视图：Key 只保留末 4 位。"""
    if not isinstance(model, dict):
        return {}
    view = dict(model)
    view["api_key"] = mask_api_key(view.pop("api_key", ""))
    return view


# ---------------------------------------------------------------- 配置写盘
#
# Bridge（面板保存）与本地 CLI（configure_bridge_llm.py / register_native_host.py）
# 会写同一个 bridge.json；这里统一"锁 + 读-合并-写 + 原子替换"，避免：
# - 两个进程同时 RMW 造成丢更新；
# - write_text 截断后崩溃留下半截 JSON（下次 Bridge 启动 fail closed）；
# - .bak 还没写完就被下一次写入覆盖，失去回滚点。


def config_backup_path(path: str | Path) -> Path:
    """配置文件备份路径（``<name>.bak``，与历史行为一致）。"""
    path = Path(path)
    return path.with_name(path.name + ".bak")


def config_lock_path(path: str | Path) -> Path:
    path = Path(path)
    return path.with_name(path.name + ".lock")


@contextmanager
def bridge_config_lock(
    path: str | Path, *, timeout: float = 1.5
) -> Iterator[None]:
    """配置写锁（跨进程 OS 级，**fail-closed**）。

    在 ``<config>.lock`` 上取 OS 排它锁（Windows ``msvcrt.locking`` / POSIX
    ``fcntl.flock``），不依赖"锁文件是否存在"或 mtime 猜测；进程崩溃时 OS 自动
    释放，残留锁文件无害，也**绝不删除仍被有效进程持有的锁**。

    等锁上限 ``timeout`` 秒；拿不到时抛 :class:`ConfigLockError`，调用方必须放弃
    本次"读最新 → 合并 → 写回"（不能无锁 RMW，否则并发修改会丢更新）。写入仍是
    原子替换 + ``.bak`` 备份。
    """
    lock_path = config_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise ConfigLockError(f"无法打开配置锁文件 {lock_path}: {exc}") from exc
    try:
        # Windows 字节范围锁需要一个真实字节；空锁文件上先补一个 0，不截断其它内容。
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
        except OSError:
            pass
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                _try_lock_fd(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    _log(
                        f"config lock busy, refusing read-modify-write: {lock_path}"
                    )
                    raise ConfigLockError(
                        f"配置文件正在被其他进程修改（等待 {timeout:g}s 未取得锁）："
                        f"{lock_path}"
                    ) from None
                time.sleep(0.05)
        _log(f"config lock acquired: {lock_path}")
        try:
            yield
        finally:
            _unlock_fd(fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录 temp + fsync + os.replace；失败清理 temp，绝不留下半截目标文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    """原子写 JSON 配置；已有文件先原子备份到 ``<name>.bak``。

    调用方应持有 :func:`bridge_config_lock`（本函数不自己加锁，便于一次锁内
    完成"读最新 → 合并 → 写回"）。
    """
    target = Path(path)
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        try:
            _atomic_write_bytes(config_backup_path(target), target.read_bytes())
        except OSError as exc:
            raise BridgeError(f"写入配置备份失败：{exc}") from exc
    try:
        _atomic_write_bytes(target, data)
    except OSError as exc:
        raise BridgeError(f"写入本地配置失败：{exc}") from exc


# ---------------------------------------------------------------- Bridge


class _SidecarBusy(Exception):
    pass


class NativeBridge:
    """把浏览器帧转换为 Sidecar JSON-RPC，并把事件/反向请求回送浏览器。"""

    def __init__(
        self,
        transport: SidecarTransport,
        *,
        config: BridgeConfig,
        read_message: ReadMessage,
        write_message: WriteMessage,
        config_path: str | None = None,
    ) -> None:
        self.transport = transport
        self.config = config
        self._config_path = config_path
        self._read_browser = read_message
        self._write_browser_raw = write_message
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._seq = 0
        self._pump_task: asyncio.Task[None] | None = None
        self._initialized = False
        self._stopping = False
        self._sidecar_alive = False
        self._run_id: str | None = None
        #: 终态通知先于 run_started 到达的 run_id（短任务竞态）；_op_run 拿到
        #: 响应后会据此跳过迟到的 run_started，并保持运行位空闲。
        self._early_finished: set[str] = set()
        self._hello_done = False
        self._sidecar_protocol: int | None = None

    # ---------- 出站（Bridge → 浏览器） ----------

    async def _send(self, message: dict[str, Any]) -> None:
        # 单帧超限会让 Chrome 丢弃/杀 host：主动替换为截断标记，保持连接可控，
        # 同时保留 params.type / run_id / method 让扩展能识别终态并清理运行态。
        original_bytes = _frame_payload_size(message)
        if original_bytes > self.config.max_message_bytes:
            replacement = build_truncated_message(message, original_bytes)
            if _frame_payload_size(replacement) > self.config.max_message_bytes:
                # 极端小上限：再瘦身（丢 data / tool / question），终态标识仍然保留。
                replacement = build_truncated_message(
                    message, original_bytes, compact=True
                )
            message = replacement
        async with self._write_lock:
            await self._write_browser_raw(message)

    async def _send_error(self, message: str, *, code: str = "bridge_error") -> None:
        await self._send({"type": "error", "code": code, "message": message})

    # ---------- 启动 / 收尾 ----------

    async def start(self) -> None:
        """启动 Sidecar、完成 initialize，并启动事件泵。"""
        try:
            await self.transport.start()
        except OSError as exc:
            # sidecar_command 不存在 / 无执行权限：转成可见错误帧，而不是裸异常退出。
            raise BridgeError(
                f"无法启动 Sidecar：{exc}", code="sidecar_start_failed"
            ) from exc
        self._sidecar_alive = True
        self._pump_task = asyncio.create_task(self._pump_sidecar())
        result = await self._rpc("initialize", dict(self.config.initialize_params))
        if not isinstance(result, dict):
            raise BridgeError("Sidecar initialize 返回非法结果")
        protocol = result.get("protocol_version")
        if not isinstance(protocol, int) or protocol != 1:
            raise BridgeError(f"不支持的 Sidecar 协议版本：{protocol!r}")
        self._sidecar_protocol = protocol
        self._initialized = True

    async def close(self) -> None:
        self._stopping = True
        if self._initialized and self._sidecar_alive:
            try:
                await self._rpc("shutdown", {}, timeout=5.0)
            except (BridgeError, TimeoutError):
                pass
        await self.transport.close()
        self._sidecar_alive = False
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._pump_task = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(BridgeError("Bridge 已关闭"))

    async def run(self) -> None:
        """主循环：读浏览器消息直到 EOF / shutdown。"""
        try:
            await self.start()
        except BridgeError as exc:
            # 启动失败（Sidecar 协议不符 / 拉起失败）也要给扩展一个可见错误。
            await self._send_error(str(exc), code=exc.code)
            await self.close()
            return
        await self._send(
            {
                "type": "bridge_ready",
                "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
                "sidecar_protocol_version": self._sidecar_protocol,
            }
        )
        try:
            while not self._stopping:
                message = await self._read_browser()
                if message is None:
                    break
                await self._handle_browser_message(message)
        except BridgeError as exc:
            await self._send_error(str(exc))
        finally:
            await self.close()

    # ---------- Sidecar RPC ----------

    async def _rpc(
        self, method: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> Any:
        if not self._sidecar_alive:
            raise BridgeError("Sidecar 未运行", code="sidecar_not_running")
        self._seq += 1
        request_id = f"b-{self._seq}"
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        budget = RPC_TIMEOUTS.get(method, RPC_TIMEOUT_DEFAULT) if timeout is None else timeout
        try:
            await self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            try:
                return await asyncio.wait_for(future, budget)
            except TimeoutError as exc:
                # 迟到响应会在 _pump_sidecar 里按"孤儿响应"丢弃，不会污染 UI。
                raise BridgeError(
                    f"Sidecar 响应超时：{method}（>{budget:g}s）",
                    code="sidecar_timeout",
                ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def _pump_sidecar(self) -> None:
        """持续读 Sidecar：响应 resolve future；通知/反向请求转给浏览器。"""
        try:
            while True:
                message = await self.transport.receive()
                if message is None:
                    await self._send({"type": "sidecar_exit", "reason": "eof"})
                    break
                method = message.get("method")
                request_id = message.get("id")
                # 反向请求 = 有方法且有 id（approval/request、clarify/request）；
                # 通知 = 有方法但无 id（agent/event 等），两者不能混为一谈。
                if isinstance(method, str) and request_id is not None:
                    await self._handle_sidecar_request(message, method)
                    continue
                if request_id is not None and str(request_id) in self._pending:
                    future = self._pending[str(request_id)]
                    if future.done():
                        continue
                    if "error" in message:
                        payload = message.get("error") or {}
                        future.set_exception(
                            BridgeError(
                                f"Sidecar 错误 {payload.get('code')}: "
                                f"{payload.get('message')}"
                            )
                        )
                    else:
                        future.set_result(message.get("result"))
                    continue
                if request_id is not None:
                    # 孤儿响应（RPC 超时 / 取消后才到）：绝不当作通知转发给浏览器。
                    _log(f"drop orphan sidecar response id={request_id!r}")
                    continue
                # 其余为通知（agent/event、agent/cancelled、agent/error 等）。
                params = message.get("params") or {}
                if isinstance(params, dict):
                    # 先结算运行位再 await 转发：否则短任务里 _op_run 可能在
                    # 我们 await 写浏览器期间才设置 _run_id（NB-1 竞态）。
                    self._maybe_finish_run(method, params)
                await self._send(
                    {
                        "type": "notification",
                        "method": method,
                        "params": params,
                    }
                )
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as exc:  # noqa: BLE001 - 传输异常转成可见状态
            await self._send({"type": "sidecar_exit", "reason": type(exc).__name__})
        finally:
            self._sidecar_alive = False
            # Sidecar 已退出：运行位与"提前完成"记录都必须作废，否则下一次 run
            # 会被误判成 run_busy（见 NB-2）。
            self._run_id = None
            self._early_finished.clear()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(BridgeError("Sidecar 已退出"))

    def _maybe_finish_run(self, method: Any, params: dict[str, Any]) -> None:
        """收到终态通知时结算运行位；终态早于 run_started 时记入 ``_early_finished``。"""
        terminal = method in ("agent/cancelled", "agent/error") or (
            method == "agent/event" and params.get("type") == "done"
        )
        if not terminal:
            return
        finished_id = params.get("run_id")
        if not isinstance(finished_id, str) or not finished_id:
            # 旧 Sidecar / 畸形通知：保守清空，避免运行位永久占住。
            self._run_id = None
            return
        if self._run_id is None:
            # 该 run 的响应还没回来（或 run_started 还没发）：记下提前完成。
            self._early_finished.add(finished_id)
            if len(self._early_finished) > 32:  # 有界，防御异常对端刷屏
                self._early_finished.pop()
            return
        if finished_id == self._run_id:
            self._run_id = None

    async def _handle_sidecar_request(
        self, message: dict[str, Any], method: str
    ) -> None:
        if method not in HOST_REQUEST_METHODS:
            await self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32601, "message": f"Bridge 不认识的方法：{method}"},
                }
            )
            return
        await self._send(
            {
                "type": "host_request",
                "request_id": message.get("id"),
                "method": method,
                "params": message.get("params") or {},
            }
        )

    # ---------- 浏览器消息 ----------

    async def _handle_browser_message(self, message: Any) -> None:
        if not isinstance(message, dict):
            await self._send_error("消息必须是 JSON object", code="invalid_request")
            return
        op = message.get("op")
        if not isinstance(op, str) or op not in ALLOWED_OPS:
            await self._send_error(f"不支持的操作：{op!r}", code="unsupported_op")
            return
        try:
            handler = getattr(self, f"_op_{op}")
            await handler(message)
        except BridgeError as exc:
            await self._send_error(str(exc), code=exc.code)
        except Exception as exc:  # noqa: BLE001 - 命令失败不能拖垮连接
            await self._send_error(f"内部错误：{type(exc).__name__}", code="internal")

    async def _op_hello(self, message: dict[str, Any]) -> None:
        protocol = message.get("protocol")
        if not isinstance(protocol, int) or isinstance(protocol, bool):
            raise BridgeError("hello.protocol 必须是整数")
        if protocol != BRIDGE_PROTOCOL_VERSION:
            await self._send(
                {
                    "type": "hello_rejected",
                    "expected": BRIDGE_PROTOCOL_VERSION,
                    "received": protocol,
                }
            )
            return
        self._hello_done = True
        model_config = self.config.initialize_params.get("model") or {}
        mcp_config = self.config.initialize_params.get("mcp") or []
        await self._send(
            {
                "type": "hello_ok",
                "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
                "sidecar_protocol_version": self._sidecar_protocol,
                # 只回显非敏感信息，供面板显示"当前用的哪个模型/接了哪些工具"。
                "model": (
                    str(model_config.get("model", ""))
                    if isinstance(model_config, dict)
                    else ""
                ),
                "base_url": (
                    str(model_config.get("base_url", ""))
                    if isinstance(model_config, dict)
                    else ""
                ),
                "mcp_servers": [
                    str(item.get("alias", ""))
                    for item in mcp_config
                    if isinstance(item, dict)
                ],
                "capabilities": {
                    "streaming": True,
                    "approval": True,
                    "clarify": True,
                    "single_run": True,
                },
            }
        )

    async def _op_run(self, message: dict[str, Any]) -> None:
        self._require_hello()
        # Sidecar 已退出时优先报"未运行"，不要拿残留的 _run_id 误报 run_busy。
        if not self._sidecar_alive:
            raise BridgeError(
                "Sidecar 未运行，请重新打开侧边栏重连", code="sidecar_not_running"
            )
        task = message.get("task")
        if not isinstance(task, str) or not task.strip():
            raise BridgeError("run.task 必须是非空字符串")
        if len(task) > self.config.max_task_chars:
            raise BridgeError(f"任务文本过长（> {self.config.max_task_chars} 字符）")
        if self._run_id is not None:
            raise BridgeError(
                "已有任务在运行；请先取消或等待完成", code="run_busy"
            )
        result = await self._rpc("agent/run", {"task": task.strip()})
        run_id = str((result or {}).get("run_id", ""))
        if not run_id:
            raise BridgeError("Sidecar 未返回 run_id")
        if run_id in self._early_finished:
            # 终态通知先于 run_started 到达（短任务竞态）：该 run 已结束，
            # 运行位保持空闲，也不再补发会误导 UI 的 run_started。
            self._early_finished.discard(run_id)
            _log(f"run {run_id} finished before run_started; skip late run_started")
            return
        self._run_id = run_id
        await self._send({"type": "run_started", "run_id": run_id})

    async def _op_cancel(self, message: dict[str, Any]) -> None:
        self._require_hello()
        run_id = message.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise BridgeError("cancel.run_id 必须是非空字符串")
        result = await self._rpc("agent/cancel", {"run_id": run_id})
        await self._send(
            {
                "type": "run_cancel_requested",
                "run_id": run_id,
                "cancelled": bool((result or {}).get("cancelled")),
            }
        )

    async def _op_host_response(self, message: dict[str, Any]) -> None:
        self._require_hello()
        request_id = message.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise BridgeError("host_response.request_id 必须是非空字符串")
        if not (request_id.startswith("srv-")):
            raise BridgeError("host_response 只能回应 Sidecar 的反向请求")
        if "error" in message:
            await self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32603,
                        "message": str(message.get("error") or "host error"),
                    },
                }
            )
            return
        result = message.get("result")
        await self.transport.send(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        )

    async def _op_ping(self, _message: dict[str, Any]) -> None:
        await self._send(
            {
                "type": "pong",
                "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
            }
        )

    async def _op_status(self, _message: dict[str, Any]) -> None:
        await self._send(
            {
                "type": "status",
                "initialized": self._initialized,
                "hello_done": self._hello_done,
                "sidecar_alive": self._sidecar_alive,
                "sidecar_protocol_version": self._sidecar_protocol,
                "active_run_id": self._run_id,
            }
        )

    async def _op_shutdown(self, _message: dict[str, Any]) -> None:
        self._stopping = True
        await self._send({"type": "shutdown_ack"})

    # ---------- LLM 配置（面板入口；Key 只写本地配置文件） ----------

    async def _op_get_config(self, _message: dict[str, Any]) -> None:
        self._require_hello()
        await self._send(
            {
                "type": "llm_config",
                "model": masked_model(self.config.initialize_params.get("model")),
                "config_path": str(self._config_path or ""),
            }
        )

    async def _op_configure_llm(self, message: dict[str, Any]) -> None:
        self._require_hello()
        if not self._config_path:
            raise BridgeError("Bridge 未从配置文件启动，无法保存 LLM 配置")
        path = Path(self._config_path)
        try:
            model = await asyncio.to_thread(self._apply_llm_config, path, message)
        except BridgeError:
            raise
        except OSError as exc:
            raise BridgeError(f"写入本地配置失败：{exc}") from exc
        # 同一次连接里更新内存视图，下次 hello/get_config 显示新模型（运行中的 Sidecar 不变）。
        self.config.initialize_params["model"] = model
        await self._send(
            {
                "type": "config_saved",
                "model": masked_model(model),
                "note": (
                    "已保存到本机配置；重启浏览器或重新打开侧边栏"
                    "（最后一个面板关闭会重启 Bridge）后生效"
                ),
            }
        )

    @staticmethod
    def _string_field(message: dict[str, Any], name: str) -> str:
        value = message.get(name)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise BridgeError(f"configure_llm.{name} 必须是字符串")
        return value.strip()

    @staticmethod
    def _check_base_url(value: str) -> None:
        """base_url 只允许 http/https；拒绝 file:/javascript: 等 scheme 与内嵌凭据。"""
        try:
            parts = urlsplit(value)
        except ValueError as exc:
            raise BridgeError(
                f"base_url 不是合法 URL：{exc}", code="invalid_base_url"
            ) from exc
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise BridgeError(
                "base_url 只允许 http/https 且必须包含主机名",
                code="invalid_base_url",
            )
        if "@" in parts.netloc:
            raise BridgeError(
                "base_url 不能包含用户名/密码（凭据只放在 api_key 字段）",
                code="invalid_base_url",
            )

    def _apply_llm_config(self, path: Path, message: dict[str, Any]) -> dict[str, Any]:
        """锁内"读最新 → 校验 → 合并 → 原子写"；返回合并后的 model（仅留内存）。"""
        with bridge_config_lock(path):
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(current, dict):
                    raise ValueError("顶层不是 object")
            except FileNotFoundError:
                current = {
                    "allowed_origins": sorted(self.config.allowed_origins),
                    "initialize": dict(self.config.initialize_params),
                    "max_task_chars": self.config.max_task_chars,
                }
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise BridgeError(f"读取本地配置失败：{exc}") from exc

            initialize = current.setdefault("initialize", {})
            if not isinstance(initialize, dict):
                raise BridgeError("本地配置 initialize 不是 object")
            model = dict(initialize.get("model") or {})
            model.setdefault("provider", "openai-compatible")

            api_key = message.get("api_key")
            if api_key is not None and not isinstance(api_key, str):
                raise BridgeError("configure_llm.api_key 必须是字符串")
            new_key = api_key.strip() if isinstance(api_key, str) else ""

            new_base = self._string_field(message, "base_url")
            new_provider = self._string_field(message, "provider")
            old_base = str(model.get("base_url") or "").strip()
            old_provider = str(model.get("provider") or "openai-compatible").strip()
            redirect = bool(new_base and new_base != old_base) or bool(
                new_provider and new_provider != old_provider
            )
            if redirect and not new_key:
                # 防凭据改道：旧 Key 绝不能被送到新端点，换端点必须同时出示新 Key。
                raise BridgeError(
                    "修改 base_url/provider 必须同时提供新的 api_key"
                    "（旧 Key 不会被发送到新端点）",
                    code="api_key_required",
                )
            if new_base:
                self._check_base_url(new_base)
            if new_provider:
                model["provider"] = new_provider
            for name in ("base_url", "model", "strong_model"):
                value = self._string_field(message, name)
                if value:
                    model[name] = value
            if new_key:
                model["api_key"] = new_key
            initialize["model"] = model
            atomic_write_json(path, current)
        return model

    def _require_hello(self) -> None:
        if not self._hello_done:
            raise BridgeError("请先发送 hello 完成协议握手")


# ---------------------------------------------------------------- CLI 入口


def load_bridge_config(path: str | None) -> BridgeConfig:
    """从本地 JSON 文件读配置；文件缺失/非法一律 fail closed。"""
    if not path:
        return BridgeConfig()
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise BridgeError("bridge 配置必须是 JSON object")
    origins = raw.get("allowed_origins") or []
    if not isinstance(origins, list) or not all(
        isinstance(item, str) and item.strip() for item in origins
    ):
        raise BridgeError("allowed_origins 必须是字符串数组")
    initialize = raw.get("initialize") or {}
    if not isinstance(initialize, dict):
        raise BridgeError("initialize 必须是 object")
    max_task_chars = raw.get("max_task_chars", 4000)
    if (
        isinstance(max_task_chars, bool)
        or not isinstance(max_task_chars, int)
        or max_task_chars <= 0
    ):
        raise BridgeError("max_task_chars 必须是正整数")
    sidecar_command = raw.get("sidecar_command") or []
    if not isinstance(sidecar_command, list) or not all(
        isinstance(item, str) and item.strip() for item in sidecar_command
    ):
        raise BridgeError("sidecar_command 必须是非空字符串数组（或省略）")
    return BridgeConfig(
        allowed_origins=frozenset(item.strip().rstrip("/") for item in origins),
        initialize_params=dict(initialize),
        max_task_chars=max_task_chars,
        sidecar_command=tuple(item.strip() for item in sidecar_command),
    )


def default_config_path() -> str:
    """Chrome 拉起 native host 时只传 origin、不传配置路径，因此需要默认位置。

    Windows: ``%LOCALAPPDATA%\\YAI\\bridge.json``；其它平台回退到
    ``$XDG_CONFIG_HOME/../YAI/bridge.json`` 或 ``~/.config/YAI/bridge.json``。
    """
    import os
    from pathlib import Path

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return str(Path(base) / "YAI" / "bridge.json")
    return str(Path.home() / ".config" / "YAI" / "bridge.json")


def default_log_path() -> str:
    """Bridge 本地日志路径（Chrome 看不到 native host 的 stderr，排障靠它）。

    写在配置同目录下：``%LOCALAPPDATA%\\YAI\\bridge.log``；写失败时静默忽略，
    绝不因为日志问题影响协议。
    """
    import os
    from pathlib import Path

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return str(Path(base) / "YAI" / "bridge.log")
    return str(Path.home() / ".config" / "YAI" / "bridge.log")


def resolve_config_path(args: list[str], env_path: str | None = None) -> str:
    """按优先级解析配置路径；忽略 Chrome 追加的 ``--parent-window`` 等参数。"""
    explicit = args[1] if len(args) > 1 and not args[1].startswith("--") else None
    return explicit or env_path or default_config_path()


def _log(message: str) -> None:
    try:
        from datetime import UTC, datetime
        from pathlib import Path

        path = Path(default_log_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(UTC).isoformat()} {message}\n")
    except Exception:  # noqa: BLE001 - 日志是旁路，绝不反噬协议
        pass


async def main(argv: list[str] | None = None) -> int:
    """``python -m yai_core.sidecar.native_bridge`` 的入口。

    Chrome 调起 native host 时 argv[1] 是扩展来源，并可能追加自己的参数
    （Windows 实测 `--parent-window=0`）；配置路径优先级：
    显式非 `--` 开头的 argv[2] → 环境变量 ``YAI_BRIDGE_CONFIG`` →
    :func:`default_config_path`。
    """
    import os

    args = list(sys.argv[1:] if argv is None else argv)
    origin = args[0] if args else ""
    config_path = resolve_config_path(args, os.environ.get("YAI_BRIDGE_CONFIG"))
    try:
        config = load_bridge_config(config_path)
        config.check_origin(origin)
    except (BridgeError, OSError, ValueError) as exc:
        _log(f"refuse start origin={origin!r} config={config_path!r} error={exc}")
        print(f"[yai-bridge] 拒绝启动：{exc}", file=sys.stderr)
        return 2
    _log(f"start origin={origin!r} config={config_path!r} frozen={getattr(sys, 'frozen', False)}")

    if getattr(sys, "frozen", False) and not config.sidecar_command:
        _log("refuse start: frozen without sidecar_command")
        print(
            "[yai-bridge] 冻结的 Bridge 必须配置 sidecar_command"
            "（指向可运行 yai_core.sidecar 的 Python 解释器）",
            file=sys.stderr,
        )
        return 2

    transport = SubprocessSidecarTransport(list(config.sidecar_command) or None)
    from functools import partial

    bridge = NativeBridge(
        transport,
        config=config,
        read_message=partial(_read_stdin_message),
        write_message=partial(_write_stdout_message),
        config_path=config_path,
    )
    try:
        await bridge.run()
    except BridgeOriginError as exc:  # pragma: no cover - start 前已校验
        _log(f"origin error: {exc}")
        print(f"[yai-bridge] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 记录后以非零码退出，Chrome 会报通信失败
        import traceback

        _log(f"fatal: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        print(f"[yai-bridge] fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    _log("exit ok")
    return 0


async def _read_stdin_message() -> dict[str, Any] | None:
    return await asyncio.to_thread(read_frame, sys.stdin.buffer)


async def _write_stdout_message(message: dict[str, Any]) -> None:
    await asyncio.to_thread(write_frame, sys.stdout.buffer, message)


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(asyncio.run(main()))
