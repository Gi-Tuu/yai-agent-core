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
