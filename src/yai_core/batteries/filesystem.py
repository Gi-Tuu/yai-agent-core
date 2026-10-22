"""文件系统资源后端：把**一个目录**安全地暴露成 :class:`~yai_core.spi.resources.ResourceProvider`。

实现的是 ``spi/resources.py`` 定义的契约，路径策略也复用那里的纯函数，
所以"内核定义的规矩"和"真正落盘的实现"不会各写一半、各漏一半。

四条实现口径：

- **写永远走 tmp + 原子替换**：任何一步失败，原文件保持不动、临时文件清掉，
  不会留半截内容给下一次读；
- **新建是"不存在才创建"**：目标已存在就报冲突，绝不静默覆盖（覆盖归 ``patch``，
  且必须带 ``expected_sha256``）；
- **改动必须带指纹**：``patch`` 先比对当前内容 sha256，不一致说明别人先改过 → 冲突，
  模型应当重读再改，而不是硬写重试；
- **IO 放线程里**：契约方法与生成的四个工具都是 async，真读写用 ``to_thread``，不卡事件循环。

已知边界（不假装解决）：``expected_sha256`` 能挡住进程内与跨进程的"读-改-写"竞态的绝大部分，
但**跨进程的严格互斥需要文件锁**，而文件锁在 Windows/NFS 上语义不一致。因此这里的承诺是
"进程内串行 + 跨进程检测冲突"；要强互斥就该由宿主加一层带锁的服务。
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import stat as stat_module
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from yai_core.spi.resources import (
    PathEscapeError,
    ResourceConflictError,
    ResourceContent,
    ResourceEntry,
    ResourceError,
    ResourceNotFoundError,
    ResourcePolicy,
    ResourceTooLargeError,
    normalize_relative,
    resolve_within_root,
    sha256_text,
)
from yai_core.types import GateDecision

_REPARSE = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


def _relative(target: Path, root: Path) -> str:
    return target.relative_to(root).as_posix()


def _is_reparse(target: Path) -> bool:
    try:
        info = target.lstat()
    except OSError:
        return False
    if stat_module.S_ISLNK(getattr(info, "st_mode", 0)):
        return True
    return bool(_REPARSE and getattr(info, "st_file_attributes", 0) & _REPARSE)


def _discard(name: str) -> None:
    try:
        os.unlink(name)
    except OSError:
        pass


class FileSystemResources:
    """以 ``root`` 为唯一可及范围的资源后端；``root`` 之外一律不可见、不可写。"""

    def __init__(
        self,
        root: str | Path,
        *,
        policy: ResourcePolicy | None = None,
        read_only: bool = False,
    ) -> None:
        base = Path(root)
        if not base.is_dir():
            raise ResourceError(f"资源根目录必须是已存在的目录：{base}")
        self.root = base.resolve()
        self.policy = policy or ResourcePolicy()
        self.read_only = read_only
        # 进程内串行化写；跨进程冲突由 expected_sha256 检测，不靠这把锁。
        self._write_lock = threading.Lock()

    # ---------- 内部：同步实现，由 async 方法丢进线程 ----------

    def _resolve(self, candidate: Any) -> Path:
        return resolve_within_root(self.root, candidate, policy=self.policy)

    def _guard_write(self) -> None:
        if self.read_only:
            raise ResourceError("该资源目录以只读方式挂载，不能写入")

    def _read_bytes(self, target: Path, path: str) -> bytes:
        if not target.exists():
            raise ResourceNotFoundError(f"文件不存在：{path}")
        if target.is_dir():
            raise ResourceError(f"{path} 是目录，不能当文件读；请用 file_list")
        # 有界读取：只读"上限 + 1"字节，靠**实际读到的字节数**判定超限。
        # 先 stat 再 read 会留一个窗口——文件在两步之间变大，就会真的读进超限内容
        # （限额是内存与带宽的护栏，不能只在"恰好没变"时成立）。
        limit = self.policy.max_read_bytes
        try:
            with target.open("rb") as handle:
                raw = handle.read(limit + 1)
        except OSError as exc:
            raise ResourceError(f"读取失败：{path}（{type(exc).__name__}）") from exc
        if len(raw) > limit:
            raise ResourceTooLargeError(
                f"文件 {path} 超过读取上限 {limit} 字节，已拒绝整读"
            )
        return raw


    def _decode(self, path: str, raw: bytes) -> ResourceContent:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResourceError(f"{path} 不是 UTF-8 文本，拒绝当文本处理") from exc
        return ResourceContent(path=path, text=text, sha256=sha256_text(text), size=len(raw))

    def _write_atomic(self, target: Path, text: str, *, exclusive: bool) -> None:
        payload = text.encode("utf-8")
        if len(payload) > self.policy.max_write_bytes:
            raise ResourceTooLargeError(
                f"待写内容 {len(payload)} 字节，超过写入上限 {self.policy.max_write_bytes} 字节"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if exclusive:
                # 硬链接是"目标存在即失败"的原子操作，比"先 exists 再 rename"可靠。
                try:
                    os.link(tmp_name, str(target))
                except FileExistsError as exc:
                    raise ResourceConflictError(
                        f"文件已存在：{_relative(target, self.root)}；"
                        "要改它请用 file_patch 并带上 file_read 给的 sha256"
                    ) from exc
                except OSError:
                    # 少数文件系统不支持硬链接：退回"检查后替换"，冲突窗口变宽但仍会报错。
                    if target.exists():
                        raise ResourceConflictError(
                            f"文件已存在：{_relative(target, self.root)}"
                        ) from None
                    os.replace(tmp_name, str(target))
                    return
                _discard(tmp_name)
                return
            os.replace(tmp_name, str(target))
        except BaseException:
            # 任何失败都不就地改原文件，也不留临时文件。
            _discard(tmp_name)
            raise

    def _sync_list(self, candidate: Any) -> tuple[Sequence[ResourceEntry], bool]:
        """列出目录内容，返回 (条目, 是否被截断)。

        内部多取一条用来判定"确实被截断"：条目数**恰好等于**上限时并没有东西被丢掉，
        标记成 truncated 是假的（会让调用方以为少看了内容）。
        """
        relative = "" if str(candidate).strip() in ("", ".", "/") else str(candidate)
        target = self.root if relative == "" else self._resolve(relative)
        if not target.exists():
            raise ResourceNotFoundError(f"目录不存在：{relative or '/'}")
        if not target.is_dir():
            raise ResourceError(f"{relative} 是文件，请用 file_read")
        cap = self.policy.max_list_entries
        entries: list[ResourceEntry] = []
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            link = _is_reparse(child)
            is_dir = child.is_dir() and not link
            # 大小用 lstat 取：不跟随符号链接，否则会**泄漏根目录外**文件的真实大小；
            # 断链（stat 抛错）按 0 计，不让一次列举整体失败。
            size = 0
            if not is_dir and not link:
                try:
                    size = child.lstat().st_size
                except OSError:
                    size = 0
            entries.append(
                ResourceEntry(
                    name=child.name,
                    path=_relative(child, self.root),
                    is_dir=is_dir,
                    size=size,
                )
            )
            if len(entries) > cap:
                # 多取一条用来告诉调用方"确实被截断了"。
                break
        return entries[:cap], len(entries) > cap

    def _sync_read(self, candidate: str) -> ResourceContent:
        target = self._resolve(candidate)
        path = _relative(target, self.root)
        return self._decode(path, self._read_bytes(target, path))

    def _sync_create(self, candidate: str, content: str) -> ResourceContent:
        self._guard_write()
        target = self._resolve(candidate)
        path = _relative(target, self.root)
        with self._write_lock:
            self._write_atomic(target, content, exclusive=True)
        return ResourceContent(
            path=path, text=content, sha256=sha256_text(content), size=len(content.encode("utf-8"))
        )

    def _sync_patch(self, candidate: str, content: str, expected_sha256: Any) -> ResourceContent:
        self._guard_write()
        if not isinstance(expected_sha256, str) or not expected_sha256.strip():
            raise ResourceError("file_patch 必须带 expected_sha256（先 file_read 拿到它）")
        target = self._resolve(candidate)
        path = _relative(target, self.root)
        with self._write_lock:
            current = self._decode(path, self._read_bytes(target, path))
            if current.sha256 != expected_sha256.strip():
                raise ResourceConflictError(
                    "内容已变化（sha256 不匹配）：请重新 file_read 取最新内容与指纹后再改"
                )
            self._write_atomic(target, content, exclusive=False)
        return ResourceContent(
            path=path, text=content, sha256=sha256_text(content), size=len(content.encode("utf-8"))
        )

    # ---------- 契约方法 ----------

    async def list(self, path: str = "") -> list[ResourceEntry]:
        entries, _truncated = await self.list_with_flag(path)
        # 用解包而不是 list(...)：类里有个叫 list 的方法，类作用域会把内置名遮住。
        return [*entries]

    async def list_with_flag(self, path: str = "") -> tuple[Sequence[ResourceEntry], bool]:
        """列出目录内容，并额外给出"是否被条数上限截断"的标志。

        契约里的 ``list()`` 只能返回条目（≤ 上限），所以"有没有被截断"需要单独一条
        出口——否则调用方只能用"长度 == 上限"去猜，恰好等于上限时会误报。
        """
        return await asyncio.to_thread(self._sync_list, path)

    async def read(self, path: str) -> ResourceContent:
        return await asyncio.to_thread(self._sync_read, path)

    async def create(self, path: str, content: str) -> ResourceContent:
        return await asyncio.to_thread(self._sync_create, path, content)

    async def patch(self, path: str, content: str, *, expected_sha256: str) -> ResourceContent:
        return await asyncio.to_thread(self._sync_patch, path, content, expected_sha256)


def filesystem_capabilities(
    root: str | Path,
    *,
    policy: ResourcePolicy | None = None,
    read_only: bool = False,
) -> list[Callable[..., Any]]:
    """产出 ``file_list/file_read/file_create/file_patch`` 四个宿主函数。

    返回的是普通闭包函数（带类型注解与 docstring），宿主直接交给
    ``AgentCore.auto`` / ``build_spec`` 即可，不需要注册胶水。
    """
    backend = FileSystemResources(root, policy=policy, read_only=read_only)

    async def file_list(path: str = "") -> dict:
        """列出目录内的文件与子目录，只返回根目录内的相对路径。

        path 是相对目录，空字符串表示根目录。超过条目上限时 truncated 为真。
        """
        entries, truncated = await backend.list_with_flag(path)
        cap = backend.policy.max_list_entries
        return {
            "path": path or "/",
            "count": len(entries),
            "truncated": truncated,
            "entries": [dataclasses.asdict(entry) for entry in entries[:cap]],
        }

    async def file_read(path: str) -> dict:
        """读取一个 UTF-8 文本文件，返回内容与 sha256 指纹。

        请留着这个 sha256：之后用 file_patch 改这个文件时必须带上它。
        """
        content = await backend.read(path)
        return {
            "path": content.path,
            "content": content.text,
            "sha256": content.sha256,
            "size": content.size,
        }

    async def file_create(path: str, content: str) -> dict:
        """新建文件，不覆盖已有文件。

        目标已存在会返回冲突：那种情况改用 file_patch，并带上 file_read 得到的 sha256。
        """
        created = await backend.create(path, content)
        return {
            "path": created.path,
            "sha256": created.sha256,
            "size": created.size,
            "created": True,
        }

    async def file_patch(path: str, content: str, expected_sha256: str) -> dict:
        """整体改写一个已存在的文件，带乐观并发校验。

        expected_sha256 必须是**你上次 file_read 看到的 sha256**。若文件在那之后被改过，
        返回冲突错误：此时应重新读取再改，而不是重试硬写。
        """
        patched = await backend.patch(path, content, expected_sha256=expected_sha256)
        return {
            "path": patched.path,
            "sha256": patched.sha256,
            "size": patched.size,
            "updated": True,
        }

    return [file_list, file_read, file_create, file_patch]


def _scope_prefix(prefix: str) -> str:
    """把宿主写的作用域前缀收敛成"目录前缀"形状（``notes`` 与 ``notes/`` 同义）。"""
    cleaned = normalize_relative(prefix) if prefix.strip() else ""
    return cleaned + "/" if cleaned and not cleaned.endswith("/") else cleaned


def _in_scope(relative: str, prefix: str) -> bool:
    if not prefix:
        # 空前缀 = **没有任何作用域**（失败关闭）。绝不等于"整个根目录"——
        # 那会让 ("",) 这样的配置悄悄把根目录整片变成可写。
        return False
    if relative == prefix.rstrip("/"):  # 直接命中目录本身（list 场景）
        return True
    return relative.startswith(prefix)


#: 风险等级序：用于"取更严的一档"，避免调用方声明的风险被默认值静默覆盖。
_RISK_RANK = {"": -1, "low": 0, "medium": 1, "high": 2}


def _stricter_risk(*values: str) -> str:
    """返回若干风险等级里**最严**的一个（未知等级按原样保留但不参与比较）。"""
    best = ""
    for value in values:
        if not value:
            continue
        if best not in _RISK_RANK or _RISK_RANK.get(value, -1) > _RISK_RANK.get(best, -1):
            best = value
    return best


def filesystem_gate(
    *,
    prefixes: tuple[str, ...] = (),
    write: bool = False,
    risk: str | None = None,
) -> Callable[[dict[str, Any], Any], GateDecision]:
    """给 ``file_*`` 工具挂的**资源作用域闸**：把授权点从"模型写来的字符串"
    移到"解析后的落点"。

    为什么需要它：``ToolExecutor`` 的准入判定与人工审批看到的都是模型原样给的
    ``path``，真正落点要等 handler 里 ``normalize_relative`` + ``resolve_within_root``
    才算出来。宿主想表达"只有 ``notes/`` 能写"，若按原始串匹配，``./notes/a.txt``、
    ``notes//a.txt``、``notes/./a.txt`` 就是**同一目标的三种写法**，白名单形同虚设。

    三件事：

    1. 用**与 handler 完全相同的** :func:`normalize_relative` 规范化——"批准的目标
       == 执行的目标"是**构造上成立**，不是靠约定；
    2. 按前缀白名单判作用域；``write=True`` 而 ``prefixes`` 为空 = **拒绝一切写**
       （失败关闭，不默认放行）；**白名单里出现空字符串前缀同样按"什么都不许"处理**，
       并在构造时直接报错——空串一旦被当成"整个根目录"，根目录整片就静默变成可写；
    3. 把规范化后的落点放进 :attr:`GateDecision.target`，让它进审批话术与审计事件。

    边界（照实说，别当成完整沙箱）：

    - 前缀匹配是**字面**的。Windows/macOS 大小写不敏感、8.3 短名这类"同一文件多个
      名字"不在这里解决——``NOTES/a.txt`` 在 ``notes/`` 白名单下会被**误拒**，
      这是有意的（宁可误拒不可误放），要放宽请宿主自己显式规范化；
    - 它**不替代**文件系统那两道闸（逐段拦链接 + realpath 复核）。真正的兜底是
      "**每次调用都重新判定**"：路径在**解析时**与**打开时**各判一次。解析与打开
      之间仍有窗口（POSIX 有 ``O_NOFOLLOW`` 兜底，**Windows 没有**），所以这是
      "把窗口收窄"，不是"零竞态"——别把它当成"闸看住了链接"。
    - 它**不做授权**：能不能调用 ``file_patch`` 仍由 ``PermissionPolicy`` 决定，
      闸只能把闸门往上抬，永远不能替宿主放行。
    """
    scope = ",".join(_scope_prefix(p) for p in prefixes) or ("*" if not write else "")
    # 构造期就拒掉空串前缀：它是"整个根目录"与"什么都不许"之间最危险的一个歧义。
    for raw_prefix in prefixes:
        if not str(raw_prefix).strip():
            raise ValueError(
                "filesystem_gate 的作用域前缀不能是空字符串："
                "空串既不是'整个根目录'也不是'某个目录'，按失败关闭处理"
            )
    # 风险取"更严的一档"：写操作默认 high，但不能把调用方显式声明的 risk 静默丢掉
    # （审计链上的风险等级必须与真实声明一致）。
    declared_risk = _stricter_risk(risk or "", "high" if write else "")
    kind = "写" if write else "读"

    def gate(arguments: dict[str, Any], subject: Any = None) -> GateDecision:
        raw = (arguments or {}).get("path", "")
        if write and (not isinstance(raw, str) or not raw.strip()):
            return GateDecision(
                reject=f"写操作必须给出非空的 path，得到 {raw!r}", risk=declared_risk, scope=scope
            )
        if not write and isinstance(raw, str) and not raw.strip():
            # file_list 的默认值就是"根目录"，读根目录不需要作用域白名单之外的事
            return GateDecision(target="", risk=declared_risk, scope=scope)
        try:
            relative = normalize_relative(raw)
        except ResourceError as exc:
            # 非法形状在这里就拒，不等 handler——理由要回灌给模型，它才知道怎么改
            return GateDecision(
                reject=f"{kind}资源路径非法，已拒绝：{exc}", risk=declared_risk, scope=scope
            )
        if not any(_in_scope(relative, _scope_prefix(p)) for p in prefixes):
            return GateDecision(
                reject=(
                    f"{relative!r} 不在允许的{kind}作用域 {sorted(prefixes)} 内，已拒绝"
                ),
                risk=declared_risk,
                scope=scope,
            )
        return GateDecision(target=relative, risk=declared_risk, scope=scope)

    return gate


__all__ = [
    "FileSystemResources",
    "PathEscapeError",
    "ResourceConflictError",
    "ResourceError",
    "ResourceNotFoundError",
    "ResourcePolicy",
    "ResourceTooLargeError",
    "filesystem_capabilities",
    "filesystem_gate",
]
