"""Native Bridge 的本地配置：来源校验、脱敏、原子写、加载。

从 ``sidecar/native_bridge.py`` 拆出；``native_bridge`` 继续 re-export。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yai_core.fsutil import replace_with_retry
from yai_core.sidecar.bridge_errors import (
    BridgeError,
    BridgeOriginError,
    ConfigLockError,
    _try_lock_fd,
    _unlock_fd,
)
from yai_core.sidecar.framing import MAX_MESSAGE_BYTES


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
        replace_with_retry(str(tmp), str(path))
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
