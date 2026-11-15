"""Bridge 层可安全回给扩展的错误。"""

from __future__ import annotations

import importlib
import os


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
