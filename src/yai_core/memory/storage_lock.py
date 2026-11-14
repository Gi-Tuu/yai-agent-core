"""MemoryVault 的跨进程/进程内写锁。

从 ``memory/vault.py`` 拆出；``vault`` 继续 re-export 这些名字，旧导入路径保持可用。
"""

from __future__ import annotations

import importlib
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_SUFFIX = ".lock"

#: 等锁上限（秒）与轮询间隔：拿不到锁就降级继续，绝不把任务卡死。
LOCK_TIMEOUT_SECONDS = 2.0
LOCK_POLL_SECONDS = 0.02

#: 进程内互斥表：key = 解析后的绝对路径。
#: 为什么 OS 锁之外还要它：Windows 的字节范围锁是按**进程**持有的，同进程里另一个
#: 句柄并不能可靠地互斥自己；而线程池（uvicorn 等）会并发调用同一个实例，同一进程内
#: 的"重读→合并→原子写"也必须串行。条目数 = 用过的 vault 文件数（每租户一个路径时是
#: 常数级），不会随运行时间增长。
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_path(path: Path) -> Path:
    """数据文件对应的锁文件路径：``<storage>.lock``。"""
    return path.with_name(path.name + LOCK_SUFFIX)


def _process_lock_for(path: Path) -> threading.Lock:
    """取该存储路径在本进程内的互斥锁（同路径永远同一把）。"""
    try:
        key = str(path.resolve())
    except OSError:  # pragma: no cover - 路径解析失败时退回字面路径
        key = str(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def _try_lock(fd: int) -> None:
    """非阻塞地取一把排它锁；被占用时抛 ``OSError``（调用方负责重试/降级）。

    平台模块用 ``importlib`` 动态取：直接在分支里 ``import`` 会让跨平台 mypy 拿另一个
    平台的桩去做属性检查（Windows 的 fcntl 桩没有 ``flock``、Linux 的 msvcrt 桩没有
    ``locking``），动态导入 + 属性访问让类型检查只看契约、不看平台桩。
    """
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":  # pragma: no cover - Windows 专用分支
        msvcrt = importlib.import_module("msvcrt")
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl = importlib.import_module("fcntl")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    """释放 :func:`_try_lock` 取的锁（尽力而为；关 fd 时 OS 也会兜底释放）。"""
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


@contextmanager
def _file_lock(
    path: Path,
    *,
    timeout: float | None = None,
    poll: float | None = None,
) -> Iterator[bool]:
    """对 ``<path>.lock`` 取写锁（进程内 + 跨进程），``yield`` 是否真的拿到锁。

    临界区由调用方决定（``MemoryVault._save`` 把"重读 → 合并 → 原子写"整段放进去）。
    **非阻塞重试**：等满 ``timeout``（默认 :data:`LOCK_TIMEOUT_SECONDS`）仍拿不到就
    ``yield False``——调用方照样落盘，退回"无锁 + mtime/size 合并"的旧口径，绝不把
    任务卡住（记忆是增强，不是主流程依赖）。

    锁文件只用于加锁、不写任何数据；进程崩溃时 OS 自动释放句柄锁，残留的空
    ``.lock`` 文件无害（下次打开继续复用）。锁是 advisory 的：绕过本类直接写数据文件
    的进程不受它约束。
    """
    wait = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    interval = LOCK_POLL_SECONDS if poll is None else poll
    deadline = time.monotonic() + max(wait, 0.0)
    process_lock = _process_lock_for(path)
    process_acquired = process_lock.acquire(timeout=max(wait, 0.0))
    fd: int | None = None
    locked = False
    try:
        if process_acquired:
            try:
                lock_file = _lock_path(path)
                lock_file.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o600)
            except OSError:
                # 连锁文件都建不出来（只读目录 / 目录被删）：按未加锁继续；
                # 真正的写如果也要失败，会由 _atomic_write 抛给调用方。
                fd = None
            while fd is not None:
                try:
                    _try_lock(fd)
                except OSError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(interval, remaining))
                    continue
                locked = True
                break
        yield locked
    finally:
        if locked and fd is not None:
            _unlock(fd)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - 关不掉也没有别的选择
                pass
        if process_acquired:
            process_lock.release()
