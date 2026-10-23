"""资源提供者契约（第 10 个 SPI）与内核侧路径策略。

内核只定义**协议 + 纯策略**，不碰文件系统：真正的 IO 在
``batteries/filesystem.py`` 或宿主自己的 provider 里。这样"能力"与"能不能碰到磁盘"
是两件事——内核不会因为多了一个契约就获得读盘权限。

三条设计口径：

1. **路径策略是纯函数**：:func:`normalize_relative` 与 :func:`resolve_within_root`
   只做形状校验与归属判定（后者会 ``resolve()``，因此读文件系统但不写），
   任何 provider 都能复用同一套规则，避免每个实现各写一半、各漏一半；
2. **越权即拒，不"清洗后放行"**：``../``、绝对路径、盘符、UNC、符号链接逃逸一律抛错。
   把 ``..`` 悄悄剥掉继续跑，等于告诉模型"越界会被温和纠正"，而真实后果是读到别人的文件；
3. **限额显式**：读/写/列举各有上限，超限报错而不是截断——截断后的文件内容看起来是完整的，
   会让模型基于半份内容做判断。
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: 相对路径的最大段数与总长度（防御性上限，正常业务路径远不到这里）。
MAX_PATH_SEGMENTS = 16
MAX_PATH_LENGTH = 512

_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_REPARSE_ATTRIBUTE = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


class ResourceError(ValueError):
    """资源层的统一基类；继承 ValueError 让工具结果天然成为"可回灌的失败"。"""


class PathEscapeError(ResourceError):
    """路径试图越出授权根目录，或形状本身非法（绝对路径、盘符、符号链接等）。"""


class ResourceNotFoundError(ResourceError):
    """资源不存在（与"存在但读不了"分开，便于宿主判断是否越权探测）。"""


class ResourceTooLargeError(ResourceError):
    """超出读/写限额。"""


class ResourceConflictError(ResourceError):
    """乐观并发冲突：内容已被别人改过，或目标已存在。"""


@dataclass(frozen=True)
class ResourcePolicy:
    """一个 provider 的限额与开关；默认值偏保守（不盲覆盖、不跟符号链接）。"""

    max_read_bytes: int = 200_000
    max_write_bytes: int = 200_000
    max_list_entries: int = 200
    allow_symlinks: bool = False


@dataclass(frozen=True)
class ResourceEntry:
    """目录条目：只给名字与元信息，不给绝对路径（绝对路径本身就是越权信息）。"""

    name: str
    path: str
    is_dir: bool
    size: int


@dataclass(frozen=True)
class ResourceContent:
    """文本内容与内容指纹；``sha256`` 供乐观并发校验回传。"""

    path: str
    text: str
    sha256: str
    size: int


@runtime_checkable
class ResourceProvider(Protocol):
    """宿主或电池层提供的资源后端；全部异步，IO 不阻塞事件循环。"""

    async def list(self, path: str = "") -> list[ResourceEntry]: ...

    async def read(self, path: str) -> ResourceContent: ...

    async def create(self, path: str, content: str) -> ResourceContent: ...

    async def patch(
        self, path: str, content: str, *, expected_sha256: str
    ) -> ResourceContent: ...


def sha256_text(text: str) -> str:
    """内容指纹（UTF-8）；写回给模型，下一次改动带上它才算"我看过的这一版"。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _has_reserved_segment(parts: list[str]) -> str | None:
    for part in parts:
        head = part.split(".", 1)[0].lower()
        if head in _WINDOWS_RESERVED:
            return part
    return None


def open_no_follow(path: Path, *, mode: str = "rb") -> int:
    """打开文件，且**不跟随最后一段符号链接**（POSIX 有效）。

    为什么需要它：路径校验（逐段 ``lstat`` + ``realpath`` 复核）与真正 ``open`` 之间
    存在窗口——攻击者可以在校验之后、打开之前把最后一段换成指向别处的链接。
    ``O_NOFOLLOW`` 把这个窗口在**内核层**关掉：目标是链接时直接失败（``ELOOP``）。

    **平台差异（必须知道）**：``O_NOFOLLOW`` 是 POSIX 扩展，**Windows 没有**
    （``os.O_NOFOLLOW`` 不存在），那边只能靠"打开前后各判一次"把窗口收窄，
    不能根除。跨平台一致的安全承诺因此只到"POSIX 上最后一段无窗口、
    Windows 上窗口被收窄"。

    另外它**只保护最后一段**：父目录在校验后被换成链接仍会把打开引到别处。
    调用方打开后应再用 :func:`fd_matches_path` 复核句柄真实路径。
    """
    flags = os.O_RDONLY
    if any(token in mode for token in ("w", "a", "+", "x")):
        flags = os.O_WRONLY | os.O_CREAT
    if "x" in mode:
        flags |= os.O_EXCL
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags | nofollow)


def _fd_path(fd: int) -> Path | None:
    """尽力取已打开句柄的真实路径；平台取不到时返回 None。"""
    if os.name == "nt":  # pragma: no cover - Windows 专用分支
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(
            ctypes.c_void_p(handle), buffer, len(buffer), 0
        )
        if not length or length >= len(buffer):
            return None
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)

    try:  # Linux：句柄路径最可靠
        actual = os.readlink(f"/proc/self/fd/{fd}")
        if actual.endswith(" (deleted)"):
            actual = actual[: -len(" (deleted)")]
        return Path(actual)
    except OSError:
        pass
    try:  # macOS
        import importlib

        fcntl = importlib.import_module("fcntl")
        getpath = getattr(fcntl, "F_GETPATH", None)
        if getpath is None:
            return None
        raw = fcntl.fcntl(fd, getpath, b"\0" * 1024)
        return Path(raw.split(b"\0", 1)[0].decode())
    except (ImportError, OSError):
        return None


def fd_matches_path(fd: int, expected: Path) -> bool:
    """打开后复核：句柄实际指向的文件必须仍是校验过的那个。

    ``O_NOFOLLOW`` 只保护**最后一段**；路径校验与 ``open`` 之间，父目录也可能被
    换成链接/重解析点，把读取引到别处。这里比对句柄的真实路径，把父目录竞态
    也收窄。平台取不到句柄路径时返回 ``True``——不制造新的失败面，但也不能
    假装已经验证过（见 :func:`open_no_follow` 的平台差异说明）。
    """
    actual = _fd_path(fd)
    if actual is None:
        return True
    return os.path.normcase(os.path.normpath(str(actual))) == os.path.normcase(
        os.path.normpath(str(expected))
    )


def normalize_relative(candidate: Any) -> str:
    """把工具入参收敛成 root 内的相对路径字符串；非法形状直接抛 :class:`PathEscapeError`。

    拒绝清单（每一条都是"看起来能用、实际跨平台会出事"的形状）：
    非字符串、空、含 NUL 或控制字符、以 ``/`` ``\\`` 开头（绝对路径与 UNC）、
    含 ``:``（盘符与 NTFS 交替数据流）、``~``、Windows 保留设备名、
    段尾点号/空格（Windows 会静默剥离，导致校验的路径与落盘的路径不是同一个）、
    过深或过长。
    """
    if not isinstance(candidate, str):
        raise PathEscapeError(f"路径必须是字符串，得到 {type(candidate).__name__}")
    text = candidate.strip()
    if not text:
        raise PathEscapeError("路径不能为空")
    if len(text) > MAX_PATH_LENGTH:
        raise PathEscapeError(f"路径长度不得超过 {MAX_PATH_LENGTH}")
    if any(ord(ch) < 32 for ch in text):
        raise PathEscapeError("路径含控制字符")
    if "\\" in text and "/" in text:
        raise PathEscapeError("路径分隔符不能混用，统一用 /")
    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        raise PathEscapeError("不接受绝对路径或 UNC 路径，只能给根目录内的相对路径")
    if ":" in normalized:
        raise PathEscapeError("路径不能含冒号（盘符或数据流写法）")
    if normalized.startswith("~"):
        raise PathEscapeError("不接受用户目录展开")

    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts:
        raise PathEscapeError("路径不能只由 . 或 / 组成")
    if len(parts) > MAX_PATH_SEGMENTS:
        raise PathEscapeError(f"路径层级不得超过 {MAX_PATH_SEGMENTS}")
    for part in parts:
        if part == "..":
            raise PathEscapeError("不接受 .. 上跳，请在授权根目录内寻址")
        if part.endswith((".", " ")):
            raise PathEscapeError(f"路径段 {part!r} 不能以点号或空格结尾")
    reserved = _has_reserved_segment(parts)
    if reserved is not None:
        raise PathEscapeError(f"路径段 {reserved!r} 是 Windows 保留设备名")
    return "/".join(parts)


def _is_reparse_or_link(path: Path) -> bool:
    """符号链接与 Windows 重解析点（junction 等）都算"可能被引到别处"。"""
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False
    if stat_module.S_ISLNK(getattr(info, "st_mode", 0)):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(_REPARSE_ATTRIBUTE and attributes & _REPARSE_ATTRIBUTE)


def resolve_within_root(
    root: Path,
    candidate: Any,
    *,
    policy: ResourcePolicy | None = None,
) -> Path:
    """把相对路径解析到 ``root`` 之内，拒绝任何逃逸；返回绝对路径，不创建任何东西。

    两道闸，缺一不可：

    1. **逐段拦链接**：从 root 往下走，路径上每一段已存在的节点都不能是符号链接或
       重解析点。POSIX 有 ``O_NOFOLLOW`` 可兜底，**Windows 没有**，所以这一道必须自己走；
    2. **归属复核**：``resolve()`` 之后必须仍在 ``root.resolve()`` 之下
       （root 自身可能是链接，例如 macOS 的 ``/tmp``，所以两边都解析）。
    """
    rules = policy or ResourcePolicy()
    relative = normalize_relative(candidate)
    root_resolved = Path(root).resolve()
    parts = relative.split("/")

    target = root_resolved
    for part in parts:
        target = target / part
        if not rules.allow_symlinks and _is_reparse_or_link(target):
            raise PathEscapeError(f"路径段 {part!r} 是链接或重解析点，已拒绝（防逃逸）")

    resolved = Path(os.path.realpath(target))
    if not resolved.is_relative_to(root_resolved):
        raise PathEscapeError("解析后的路径越出了授权根目录")
    return resolved
