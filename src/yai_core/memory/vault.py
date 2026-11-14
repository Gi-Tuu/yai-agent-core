"""结构化长期记忆：remember / recall 两件套，JSON 原子落盘，可选语义召回。

设计原则（与内核其它部分一致）：

- **零第三方硬依赖**：默认只用标准库做词法相关度检索，JSON 原子落盘；
- **语义是增强、不是必需**：宿主经第八个 SPI（``EmbeddingProvider``）注入嵌入器后，
  召回在词法通道之外并行开一条语义通道（余弦相似度），擅长"同义改写、跨语言、
  概括描述"——词法一个字都对不上、语义上却相关的记忆也能被找回；
- **embedder 缺失或抛错时自动降级纯词法**：记录在 ``semantic_last_error``，
  绝不让"记忆增强"反过来拖垮主流程；
- **同步 / 异步双轨**：:meth:`MemoryVault.recall` 保持同步、纯词法（向后兼容，
  宿主在非 async 上下文也能直接查）；:meth:`MemoryVault.arecall` 是异步入口，
  词法 + 可选语义。注册给模型的 ``recall`` 工具与"任务起始主动召回"都走 ``arecall``。

记忆条目是带类型 / 标签 / 重要度的结构化笔记（事实、偏好、画像、经历、备注），
而不是把整段对话塞进向量库——长期记忆应当是被提炼过、可被检索的"用户模型"。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yai_core.diagnostics import (
    DiagnosticEvent,
    DiagnosticKind,
    DiagnosticLevel,
    DiagnosticSink,
    emit,
)
from yai_core.discovery.scoring import (
    DEFAULT_SYNONYM_GROUPS,
    canonicalize,
    tokenize,
)
from yai_core.discovery.semantic import cosine_similarity
from yai_core.fsutil import replace_with_retry
from yai_core.memory.constants import DEFAULT_RECALL_K, VALID_KINDS
from yai_core.memory.note import (
    MemoryNote,
    _default_clock,
    _index_tokens,
)
from yai_core.memory.storage_lock import (
    LOCK_POLL_SECONDS,
    LOCK_TIMEOUT_SECONDS,
    _lock_path,
)
from yai_core.memory.storage_lock import (
    _file_lock as _storage_file_lock,
)
from yai_core.spi.cipher import StorageCipher
from yai_core.spi.embedding import EmbeddingProvider

#: 落盘格式版本，后续结构演进时据此迁移。
STORAGE_VERSION = 1



#: 语义召回门槛，口径与 discovery.semantic.DEFAULT_SCORE_THRESHOLD 一致
#: （本地 bge-m3 实测：完全无关 query 余弦约 0.30~0.32，语义相关约 0.48~0.52）。
#: 记忆返回的是 top-k 多条而非"只选一个"，因此不设工具发现里的 margin 判定。
SEMANTIC_RECALL_THRESHOLD = 0.45

#: 重复记忆判定阈值：新增与既有记忆相关度达到该值视为同一条，走合并而非新增。
DEDUP_SIMILARITY = 0.92

#: 加载失败的分类（``MemoryVault.status()["storage"]["status"]`` 的取值）。
LOAD_OK = "ok"
LOAD_MISSING = "missing"
LOAD_UNREADABLE = "unreadable"
LOAD_CORRUPT = "corrupt"
LOAD_DECRYPT_FAILED = "decrypt_failed"
LOAD_VERSION_INCOMPATIBLE = "version_incompatible"
#: 宿主声明的 owner 与文件内记录的 owner 不一致：拒绝读取，避免跨租户泄漏。
LOAD_OWNER_MISMATCH = "owner_mismatch"

#: 加载失败时的处置策略：
#: - ``quarantine``（默认）：把原始字节复制成同目录 ``<name>.corrupt-<时间戳>``
#:   备份后按空库继续（写入可用）；复制失败则自动降级只读，绝不覆盖原文件。
#: - ``read_only``：读接口可用（返回空库），写入接口抛
#:   :class:`MemoryVaultReadOnlyError`，原文件分毫不动，等宿主修复。
#: - ``raise``：构造器直接抛 :class:`MemoryVaultLoadError`，由宿主决定恢复方式。
CORRUPTION_POLICIES = ("quarantine", "read_only", "raise")

#: quarantine 备份保留上限：每次加载失败都留一份原始字节，反复崩溃重启不能无限堆积。
#: 超过上限时删掉最旧的备份（只删本 vault 自己按 ``<name>.corrupt-<ts>`` 命名的文件）。
QUARANTINE_KEEP = 5

#: 自定义诊断种类（``DiagnosticKind`` 内置枚举不含它们；按 diagnostics 文档，实现方
#: 可用自定义字符串）。加载失败仍沿用内置的 ``memory_load_failed``。
DIAG_MEMORY_SAVE_FAILED = "memory_save_failed"
DIAG_MEMORY_DISK_MODIFIED = "memory_disk_modified"
DIAG_MEMORY_EVICTED = "memory_evicted"
#: 等写锁超时、降级为无锁写入（仍有 mtime/size 合并兜底，但要让运维看得见）。
DIAG_MEMORY_LOCK_TIMEOUT = "memory_lock_timeout"

#: "加载时文件还不存在"的磁盘指纹：之后文件被别的实例建出来也算外部修改，必须先合并。
_MISSING_DISK_SIGNATURE = (-1, -1)

# ---------- 跨进程写锁 ----------

#: 锁文件后缀：数据文件 ``x.json`` 的锁文件是 ``x.json.lock``（同目录，不参与数据）。
@contextmanager
def _file_lock(
    path: Path,
    *,
    timeout: float | None = None,
    poll: float | None = None,
) -> Iterator[bool]:
    """兼容包装：默认超时仍取 ``vault.LOCK_TIMEOUT_SECONDS``，便于宿主/测试注入。"""
    with _storage_file_lock(
        path,
        timeout=LOCK_TIMEOUT_SECONDS if timeout is None else timeout,
        poll=LOCK_POLL_SECONDS if poll is None else poll,
    ) as locked:
        yield locked


class MemoryVaultError(RuntimeError):
    """长期记忆存储层错误基类。"""


class MemoryVaultLoadError(MemoryVaultError):
    """落盘记忆加载失败（损坏 / 解密失败 / 版本不兼容 / 不可读）。

    仅在宿主显式选择 ``on_corruption="raise"`` 时由构造器抛出；异常携带分类
    状态与路径，宿主可据此决定恢复方式（换密钥、迁移、人工修复或丢弃）。
    """

    def __init__(self, status: str, path: Path, detail: str = "") -> None:
        self.status = status
        self.path = path
        self.detail = detail
        super().__init__(f"长期记忆加载失败（{status}）：{path}（{detail}）")


class MemoryVaultReadOnlyError(MemoryVaultError):
    """记忆库处于只读降级：读接口可用，写入被拒绝。

    用于"原始档案尚未恢复"的场景，避免空库启动后一次 ``remember`` 把损坏或
    无法解密的原始文件直接覆盖掉。
    """


def _empty_load_state() -> dict[str, Any]:
    return {"status": LOAD_OK, "detail": "", "quarantined_to": None, "read_only": False}



class MemoryVault:
    """结构化长期记忆的存储与检索。

    Args:
        storage_path: 落盘 JSON 路径；None 时纯内存（不落盘）。
        cipher: 可选的落盘静态加密（第九 SPI）；None 时明文 JSON。
        embedder: 可选文本嵌入器（第八 SPI）；None 时 :meth:`arecall` 退化为纯词法。
        clock: 时间源（测试可注入固定时钟）。
        max_notes: 可选容量上限（正整数）；None（默认）表示不限制、绝不主动删除
            记忆。配置后若条数超限，按"重要度低 → 最久未用 → 最旧"淘汰到上限，
            给长期运行的数字员工一个有界存储安全阀。importance=5 只是最后才被
            淘汰、不设硬豁免，以保证容量一定能收敛。
        on_corruption: 加载失败时的处置策略，取值见 :data:`CORRUPTION_POLICIES`。
            默认 ``quarantine``：原始字节先备份成 ``<name>.corrupt-<时间戳>``，
            再按空库启动；备份失败自动转只读。``read_only`` 只允许读、写入抛错；
            ``raise`` 让构造器直接抛 :class:`MemoryVaultLoadError`。
        on_diagnostic: 可选本地诊断回调；加载失败时发一条
            ``memory_load_failed``（ERROR），带分类、路径与处置结果。
        owner: 可选的租户 / 所有者标识（v0.26 P0 记忆隔离的最小方案）。声明后
            落盘会记录 owner；下次加载时若文件里的 owner 与声明不一致，直接拒绝
            读取并只读降级（``owner_mismatch``），绝不把别的租户的记忆读进来，
            也绝不会覆盖它。旧文件没有 owner 字段时保持兼容（视为未声明）。
            反过来也 fail-closed：**文件已声明 owner 而本实例 owner=None** 时同样
            只读降级并发诊断——否则一个"忘了传 owner"的实例就能读到别的租户的记忆，
            还会在下次保存时把文件里的 owner 抹成 null。更完整的多租户模型仍由宿主
            用独立实例 / 独立路径实现。

    并发与持久化语义（v0.26 数据完整性加固）：

    - **读接口不因写盘失败而失败**：``recall`` / ``arecall`` 会刷新访问计数并尝试落盘，
      落盘失败只记 ``memory_save_failed`` 诊断与 ``dirty`` 标记，召回结果照常返回；
    - **多实例共享同一文件不静默丢更新**：每次落盘前比对磁盘指纹（mtime/size），发现
      文件被外部改过就重读并按 note id 合并（本地改动的条目优先、磁盘新增保留、本地
      ``forget`` / 容量淘汰生效），合并后原子写；外部文件已损坏则先 quarantine 再覆盖；
    - **原子写带上 durability**：临时文件 ``flush + fsync`` 后才 ``os.replace``，
      POSIX 上再 fsync 父目录——掉电后不会留下"半截/空文件"把整库记忆带走；
    - **跨进程写锁 fail-closed**：``_save`` 的"重读 → 合并 → 原子写"整段在
      ``<storage>.lock`` 的 OS 级文件锁内执行（Windows ``msvcrt.locking`` / POSIX
      ``fcntl.flock``，同进程内再加一把 ``threading.Lock``）。等锁上限
      :data:`LOCK_TIMEOUT_SECONDS`（2 秒）；超时**放弃本次磁盘写入**（绝不无锁
      read-modify-write），保留内存变更与 ``dirty``、发 ``memory_lock_timeout``
      诊断，由下一次写入或 :meth:`flush` 在安全时机重试。锁是 advisory 的：绕过
      :class:`MemoryVault` 直接改数据文件的进程不受约束。
    - 已知边界：跨实例的 ``forget`` 只保证**本实例**不会把它写回去；另一个仍持有该条目的
      实例若随后写入，会按"本地优先"把它带回来。需要跨实例删除语义时，应由宿主保证
      同一文件同一时刻只有一个写入者（或改用共享数据库实现）。
    """

    def __init__(
        self,
        *,
        storage_path: Path | str | None = None,
        cipher: StorageCipher | None = None,
        embedder: EmbeddingProvider | None = None,
        clock: Any = _default_clock,
        max_notes: int | None = None,
        on_corruption: str = "quarantine",
        on_diagnostic: DiagnosticSink | None = None,
        owner: str | None = None,
    ) -> None:
        if max_notes is not None and (
            isinstance(max_notes, bool) or not isinstance(max_notes, int) or max_notes <= 0
        ):
            raise ValueError("max_notes 必须是正整数，或为 None（不限制）")
        if on_corruption not in CORRUPTION_POLICIES:
            raise ValueError(
                f"on_corruption 必须是 {CORRUPTION_POLICIES} 之一，收到 {on_corruption!r}"
            )
        self.storage_path = Path(storage_path) if storage_path is not None else None
        self._cipher = cipher
        self._embedder = embedder
        self._clock = clock
        self._max_notes = max_notes
        self._on_corruption = on_corruption
        self._on_diagnostic = on_diagnostic
        self._owner = str(owner) if owner is not None else None
        if self._owner is not None and not self._owner.strip():
            raise ValueError("owner 必须是非空字符串或 None")
        self._notes: list[MemoryNote] = []
        # 加载状态（分类 / 详情 / 隔离备份路径 / 是否只读），供宿主运维与测试观测。
        self.load_state: dict[str, Any] = _empty_load_state()
        # 只读降级：读可用、写被拒绝，避免覆盖尚未恢复的原始档案。
        self._read_only = False
        # note_id -> 向量的懒缓存：新增 / 合并后该 id 缺失，下次 arecall 批量补算。
        self._vectors: dict[str, Sequence[float]] = {}
        # 词法倒排索引（token -> note_id 集合），懒构建：任何写入后置 None，
        # 下次召回时重建。None 也表示"尚未构建"。
        self._lex_index: dict[str, set[str]] | None = None
        # 因 max_notes 容量上限被淘汰的累计条数（含启动收敛），便于宿主观测。
        self._evicted_count = 0
        # 最近一次语义通道降级原因（None 表示未降级），便于宿主观测与测试。
        self.semantic_last_error: str | None = None
        # 内存状态是否落后于磁盘（落盘失败后置位；任何一次成功落盘清零）。
        self._dirty = False
        # 批量写入状态：>0 表示处在 batch() 内，逐条写入只标脏、不落盘。
        self._defer_persist = 0
        self._pending_persist = False
        # note id 集合：contains() 不能 O(n) 扫全表（10k 规模下 remember 会退化成 O(n²)）。
        self._ids: set[str] = set()
        # note id -> (unigram 集, bigram 集) 缓存：召回/去重不再对同一文本反复 tokenize。
        self._note_tokens: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        # 我们"认为磁盘上是什么"的指纹 (st_mtime_ns, st_size)；None = 不做外部修改检测
        # （纯内存库 / 加载时文件已损坏并隔离，本实例的内存状态就是权威版本）。
        self._disk_signature: tuple[int, int] | None = None
        # 文件里记录的 owner（None = 旧文件未声明）；保存时绝不把它写成 null。
        self._stored_owner: str | None = None
        # 本地已删除（forget / 容量淘汰）的 id：外部合并时不能被磁盘版本复活。
        self._forgotten_ids: set[str] = set()
        # 最近一次 remember 写入的条目是否当场被容量淘汰（工具侧据此回报 stored）。
        self.last_remember_evicted = False
        # 最近一次写盘是否真的持久化成功（False = 只读/锁失败/写失败，内存变更仍保留）。
        self.last_write_persisted = False
        self._load()
        self._rebuild_ids()
        # 宿主可能在运行一段时间后才调小上限：打开存量库时也收敛到容量内。
        # 只读降级时不做任何写操作（收敛结果留在内存里也不落盘）；收敛落盘失败
        # **不能**让构造器抛错（库仍可用，只是磁盘还没更新），记 dirty + 诊断即可。
        if (
            not self._read_only
            and self._max_notes is not None
            and self._enforce_cap() > 0
        ):
            self._save_safely(reason="startup_cap_convergence")

    @property
    def read_only(self) -> bool:
        """记忆库是否处于只读降级（加载失败后禁止写入）。"""
        return self._read_only

    @property
    def dirty(self) -> bool:
        """内存状态是否尚未成功落盘（上一次保存失败后为 True）。"""
        return self._dirty

    @property
    def lock_path(self) -> Path | None:
        """写锁文件路径（``<storage>.lock``）；纯内存库返回 None。

        锁文件只用来加锁、不含任何数据，可以安全地随数据文件一起备份/忽略；
        进程崩溃后残留的空锁文件不影响下一次打开。
        """
        return _lock_path(self.storage_path) if self.storage_path is not None else None

    def contains(self, note_id: str) -> bool:
        """该 id 是否仍在本库内存状态里（``remember`` 后可能已因容量上限被淘汰）。"""
        return note_id in self._ids

    def _rebuild_ids(self) -> None:
        self._ids = {note.id for note in self._notes}

    def _token_sets(self, note: MemoryNote) -> tuple[frozenset[str], frozenset[str]]:
        """按 note 缓存 (unigram, bigram)；写入/合并时失效，读路径只付一次 tokenize。"""
        cached = self._note_tokens.get(note.id)
        if cached is not None:
            return cached
        unigrams, bigrams = tokenize(
            canonicalize(note.candidate_text(), DEFAULT_SYNONYM_GROUPS)
        )
        cached = (frozenset(unigrams), frozenset(bigrams))
        self._note_tokens[note.id] = cached
        return cached

    @staticmethod
    def _similarity_from_sets(
        query_sets: tuple[frozenset[str], frozenset[str]],
        note_sets: tuple[frozenset[str], frozenset[str]],
    ) -> float:
        """与 :func:`token_similarity` 完全同公式，但复用两侧已缓存的 token 集。"""
        q_unigrams, q_bigrams = query_sets
        c_unigrams, c_bigrams = note_sets
        uni = len(q_unigrams & c_unigrams) / len(q_unigrams) if q_unigrams else 0.0
        if not q_bigrams:
            return round(uni, 4)
        bi = len(q_bigrams & c_bigrams) / len(q_bigrams)
        return round(0.4 * uni + 0.6 * bi, 4)

    def _persist(self) -> bool:
        """落盘；批量写入期间只标脏，退出 :meth:`batch` 时统一写一次。"""
        if self._defer_persist > 0:
            self._dirty = True
            self._pending_persist = True
            self.last_write_persisted = False
            return False
        return self._save()

    @contextmanager
    def batch(self) -> Iterator[MemoryVault]:
        """批量写入上下文：期间不逐条落盘，退出最外层时统一保存一次。

        内存语义与单条写入完全一致（去重 / 容量淘汰 / 倒排索引都照常维护），
        只是把 N 次全量落盘合并成 1 次；异常退出也会尝试落盘已写入的部分。
        """
        self._defer_persist += 1
        try:
            yield self
        finally:
            self._defer_persist -= 1
            if self._defer_persist == 0 and self._pending_persist:
                self._pending_persist = False
                self._save_safely(reason="batch_flush")

    def remember_many(
        self, entries: Iterable[Mapping[str, Any] | str]
    ) -> list[tuple[MemoryNote, bool]]:
        """批量写入（单次落盘）。

        每项是 ``{"content": ..., "kind": ..., "tags": [...], "importance": n}``
        或直接一个字符串；返回与 :meth:`remember` 相同的逐条结果。
        """
        results: list[tuple[MemoryNote, bool]] = []
        with self.batch():
            for entry in entries:
                if isinstance(entry, str):
                    results.append(self.remember(entry))
                else:
                    results.append(
                        self.remember(
                            str(entry.get("content", "")),
                            kind=str(entry.get("kind", "note")),
                            tags=entry.get("tags"),
                            importance=int(entry.get("importance", 3)),
                        )
                    )
        return results

    def _lex_add(self, note: MemoryNote) -> None:
        if self._lex_index is None:
            return
        for token in _index_tokens(note.candidate_text()):
            self._lex_index.setdefault(token, set()).add(note.id)

    def _lex_remove(self, note_id: str, tokens: set[str]) -> None:
        if self._lex_index is None:
            return
        for token in tokens:
            posting = self._lex_index.get(token)
            if posting is None:
                continue
            posting.discard(note_id)
            if not posting:
                self._lex_index.pop(token, None)


    # ---------- 写入 ----------

    def remember(
        self,
        content: str,
        *,
        kind: str = "note",
        tags: list[str] | None = None,
        importance: int = 3,
    ) -> tuple[MemoryNote, bool]:
        """写入一条记忆；与既有记忆高度重复时合并（标签并集、重要度取高）。

        返回 ``(note, merged)``，``merged=True`` 表示命中并合并了既有记忆。
        """
        text = (content or "").strip()
        if not text:
            raise ValueError("记忆内容不能为空")
        self._ensure_writable()
        norm_kind = kind if kind in VALID_KINDS else "note"
        norm_tags = self._normalize_tags(tags)
        norm_importance = self._normalize_importance(importance)

        duplicate = self._find_duplicate(text, norm_tags)
        if duplicate is not None:
            old_tokens = (
                _index_tokens(duplicate.candidate_text())
                if self._lex_index is not None
                else set()
            )
            merged_tags = list(dict.fromkeys([*duplicate.tags, *norm_tags]))
            duplicate.tags = merged_tags
            duplicate.importance = max(duplicate.importance, norm_importance)
            duplicate.access_count += 1
            duplicate.last_used_at = self._clock()
            # 合并可能改变正文标签 / 重要度，旧向量失效，下次语义召回时重算。
            self._vectors.pop(duplicate.id, None)
            self._note_tokens.pop(duplicate.id, None)  # 标签变了，token 缓存失效。
            # 倒排增量维护：先摘旧 token，再按新状态挂回去（不必整表重建）。
            self._lex_remove(duplicate.id, old_tokens)
            self._lex_add(duplicate)
            merged = duplicate
            did_merge = True
        else:
            note = MemoryNote(
                text,
                kind=norm_kind,
                tags=norm_tags,
                importance=norm_importance,
                created_at=self._clock(),
                last_used_at=self._clock(),
            )
            self._notes.append(note)
            self._ids.add(note.id)
            self._lex_add(note)
            merged = note
            did_merge = False

        # 配置了容量上限时在落盘前收敛（默认 None 立即返回）。淘汰可能把**刚写入的
        # 条目**也带走（新条目重要度最低时），必须让调用方知道：返回一个库里并不存在
        # 的 id 却报成功，等于把"丢档"伪装成"写入成功"。
        self._enforce_cap()
        self.last_remember_evicted = not self.contains(merged.id)
        self._persist()
        return merged, did_merge

    def forget(self, note_id: str) -> bool:
        """删除一条记忆；不存在返回 False。"""
        self._ensure_writable()
        for index, note in enumerate(self._notes):
            if note.id == note_id:
                if self._lex_index is not None:
                    self._lex_remove(note_id, _index_tokens(note.candidate_text()))
                del self._notes[index]
                self._ids.discard(note_id)
                self._vectors.pop(note_id, None)
                self._note_tokens.pop(note_id, None)
                # 记墓碑：本次落盘前若检测到外部修改，合并时不能让磁盘版本复活它。
                self._forgotten_ids.add(note_id)
                self._persist()
                return True
        return False

    # ---------- 检索（同步：纯词法，向后兼容） ----------

    def recall(
        self,
        query: str,
        *,
        k: int = DEFAULT_RECALL_K,
        kinds: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """按词法相关度检索 top-k 记忆（同步、零依赖、不需要 embedder）。

        刷新命中条目的访问计数后**尽力**落盘：落盘失败只记诊断与 ``dirty``，
        绝不让一次读取因为磁盘只读 / 被占用而失败。
        """
        now = self._clock()
        rows: list[tuple[MemoryNote, float, float]] = []
        for note, sim in self._rank_lexical(query, kinds=kinds, tags=tags):
            # 重要度只做温和加成，避免高重要度但不相关的记忆霸榜。
            score = sim * (0.75 + 0.25 * (note.importance / 5.0))
            rows.append((note, score, sim))
        rows.sort(key=lambda item: item[1], reverse=True)
        top = rows[:k]
        for note, _, _ in top:
            self._touch(note, now)
        if top:
            self._save_safely(reason="recall_touch")
        return [self._note_dict(note, round(relevance, 3)) for note, _, relevance in top]

    # ---------- 检索（异步：词法 + 可选语义） ----------

    async def arecall(
        self,
        query: str,
        *,
        k: int = DEFAULT_RECALL_K,
        kinds: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """词法与语义双通道召回；未配置 embedder 时与同步 :meth:`recall` 完全等价。

        两条通道是"独立召回 gate"而非加权平均：词法命中（精确专有名词）**或**
        语义余弦过阈值（同义改写 / 跨语言 / 概括描述）任一成立即召回，最终按
        ``max(词法, 语义)`` 再叠重要度温和加成排序。embedder 任何失败都降级纯词法。
        """
        # 未配置嵌入器：直接复用同步词法路径，保证零行为差异。
        if self._embedder is None:
            return self.recall(query, k=k, kinds=kinds, tags=tags)

        now = self._clock()
        lex_by_id = {
            note.id: sim for note, sim in self._rank_lexical(query, kinds=kinds, tags=tags)
        }
        sem_by_id: dict[str, float] = {}
        if await self._ensure_vectors():
            query_vector = await self._embed_query(query)
            if query_vector is not None:
                for note in self._notes:
                    if not self._matches_filters(note, kinds, tags):
                        continue
                    vector = self._vectors.get(note.id)
                    if vector is None:
                        continue
                    sem = cosine_similarity(query_vector, vector)
                    if sem >= SEMANTIC_RECALL_THRESHOLD:
                        sem_by_id[note.id] = sem

        matched_ids = set(lex_by_id) | set(sem_by_id)
        if not matched_ids:
            return []
        rows: list[tuple[MemoryNote, float, float]] = []
        for note in self._notes:
            if note.id not in matched_ids:
                continue
            lex = lex_by_id.get(note.id, 0.0)
            sem = sem_by_id.get(note.id, 0.0)
            relevance = lex if lex >= sem else sem
            score = relevance * (0.75 + 0.25 * (note.importance / 5.0))
            rows.append((note, score, relevance))
        rows.sort(key=lambda item: item[1], reverse=True)
        top = rows[:k]
        for note, _, _ in top:
            self._touch(note, now)
        if top:
            self._save_safely(reason="arecall_touch")
        return [self._note_dict(note, round(relevance, 3)) for note, _, relevance in top]

    # ---------- 状态 ----------

    def status(self) -> dict[str, Any]:
        by_kind = {kind: 0 for kind in VALID_KINDS}
        for note in self._notes:
            # 文件里可能有旧版 / 未来版 / 手工写入的类型：不能让 status() 因此抛 KeyError
            # （宿主的健康检查面板会整块 500）。未知类型按原样计数。
            by_kind[note.kind] = by_kind.get(note.kind, 0) + 1
        recent = [self._note_dict(note) for note in sorted(
            self._notes, key=lambda n: n.last_used_at, reverse=True
        )[:10]]
        return {
            "total": len(self._notes),
            "by_kind": by_kind,
            "semantic_enabled": self._embedder is not None,
            "semantic_last_error": self.semantic_last_error,
            "capacity": {
                "max_notes": self._max_notes,
                "evicted": self._evicted_count,
            },
            "storage": {
                "status": self.load_state["status"],
                "detail": self.load_state["detail"],
                "read_only": self._read_only,
                "quarantined_to": self.load_state["quarantined_to"],
                "owner": self._owner,
                "dirty": self._dirty,
                "persisted": self.last_write_persisted,
                "disk_synced": self._disk_signature is not None,
            },
            "recent": recent,
        }

    # ---------- 内部：筛选 / 排序 / 索引 / 容量 ----------

    @staticmethod
    def _matches_filters(
        note: MemoryNote,
        kinds: list[str] | None,
        tags: list[str] | None,
    ) -> bool:
        if kinds and note.kind not in kinds:
            return False
        if tags and not set(tags).intersection(note.tags):
            return False
        return True

    def _ensure_lex_index(self) -> dict[str, set[str]]:
        """懒构建（或重建）词法倒排索引：canonical token -> 含该 token 的 note_id 集合。

        任何写入都把 ``_lex_index`` 置 None，因此这里只在"写后第一次召回"付一次
        O(n) 建索引成本，之后每次召回只扫候选并集，不再对全部记忆逐条算相似度。
        """
        if self._lex_index is None:
            index: dict[str, set[str]] = {}
            for note in self._notes:
                for token in _index_tokens(note.candidate_text()):
                    index.setdefault(token, set()).add(note.id)
            self._lex_index = index
        return self._lex_index

    def _enforce_cap(self) -> int:
        """把记忆条数收敛到 ``max_notes`` 以内，返回本轮淘汰条数；未配置上限返回 0。

        淘汰键（全部升序、越靠前越先删）：重要度低 → 最久未用 → 最旧 → id。
        importance=5 不设硬豁免（仅排在最后），确保无论标记分布如何容量都能收敛；
        被淘汰记忆的缓存向量同步清除，并记墓碑（外部合并时不能把淘汰项复活）。
        调用方负责随后 ``_save()`` 落盘。淘汰是"删记忆"的决策，必须可观测：
        发一条 ``memory_evicted`` 诊断（红线：自适应决策不许静默）。
        """
        if self._max_notes is None or len(self._notes) <= self._max_notes:
            return 0
        ordered = sorted(
            self._notes,
            key=lambda n: (n.importance, n.last_used_at, n.created_at, n.id),
        )
        drop_count = len(self._notes) - self._max_notes
        victim_ids = {n.id for n in ordered[:drop_count]}
        if self._lex_index is not None:
            for victim in ordered[:drop_count]:
                self._lex_remove(victim.id, _index_tokens(victim.candidate_text()))
        self._notes = [n for n in self._notes if n.id not in victim_ids]
        self._ids.difference_update(victim_ids)
        for victim_id in victim_ids:
            self._vectors.pop(victim_id, None)
            self._note_tokens.pop(victim_id, None)
            self._forgotten_ids.add(victim_id)
        self._evicted_count += drop_count
        emit(
            self._on_diagnostic,
            DiagnosticEvent(
                kind=DIAG_MEMORY_EVICTED,
                level=DiagnosticLevel.WARNING,
                message=(
                    f"长期记忆超出容量上限 max_notes={self._max_notes}，"
                    f"已按 重要度低→最久未用→最旧 淘汰 {drop_count} 条，"
                    f"当前 {len(self._notes)} 条"
                ),
                data={
                    "max_notes": self._max_notes,
                    "evicted": drop_count,
                    "remaining": len(self._notes),
                },
            ),
        )
        return drop_count

    def _rank_lexical(
        self,
        query: str,
        *,
        kinds: list[str] | None,
        tags: list[str] | None,
    ) -> list[tuple[MemoryNote, float]]:
        """词法通道：返回通过 kinds/tags 过滤且词法相关度 > 0 的 (note, sim)。

        规模治理：先用倒排索引取"与 query 至少有一个公共 token"的候选并集，再只对
        候选逐条算 :func:`token_similarity`。由于公共 token 交集为空当且仅当
        ``token_similarity == 0``，被倒排跳过的记忆原本也会被 ``sim > 0`` 丢弃，
        因此结果与全表扫描逐字节一致，只是把 O(n) 全量打分降为 O(候选数)。query
        切不出任何 token（如纯标点）时回退全扫，保持与历史行为完全一致。
        """
        index = self._ensure_lex_index()
        query_tokens = _index_tokens(query)
        candidate_ids: set[str] | None = None
        if query_tokens:
            candidate_ids = set()
            for token in query_tokens:
                posting = index.get(token)
                if posting:
                    candidate_ids |= posting
        query_sets = (
            frozenset(tokenize(canonicalize(query, DEFAULT_SYNONYM_GROUPS))[0]),
            frozenset(tokenize(canonicalize(query, DEFAULT_SYNONYM_GROUPS))[1]),
        )
        ranked: list[tuple[MemoryNote, float]] = []
        for note in self._notes:  # 保持原遍历顺序，稳定排序的 tie-break 不变。
            if candidate_ids is not None and note.id not in candidate_ids:
                continue
            if not self._matches_filters(note, kinds, tags):
                continue
            sim = self._similarity_from_sets(query_sets, self._token_sets(note))
            if sim > 0:
                ranked.append((note, sim))
        return ranked

    async def _ensure_vectors(self) -> bool:
        """为尚无向量的记忆批量嵌入并缓存；embedder 失败返回 False（调用方降级词法）。"""
        if self._embedder is None:
            return False
        missing = [note for note in self._notes if note.id not in self._vectors]
        if not missing:
            return True
        try:
            vectors = await self._embedder.embed_texts([n.candidate_text() for n in missing])
        except Exception as exc:  # noqa: BLE001 - 语义是增强，失败降级词法
            self.semantic_last_error = f"{type(exc).__name__}: {exc}"
            return False
        if not isinstance(vectors, (list, tuple)) or len(vectors) != len(missing):
            self.semantic_last_error = "embedder 返回的向量数量与记忆数不一致"
            return False
        for note, vector in zip(missing, vectors, strict=True):
            self._vectors[note.id] = vector
        self.semantic_last_error = None
        return True

    async def _embed_query(self, query: str) -> Sequence[float] | None:
        """嵌入查询文本；失败返回 None（调用方降级纯词法）。"""
        if self._embedder is None:
            return None
        try:
            vectors = await self._embedder.embed_texts([query])
        except Exception as exc:  # noqa: BLE001 - 降级词法
            self.semantic_last_error = f"{type(exc).__name__}: {exc}"
            return None
        if isinstance(vectors, (list, tuple)) and len(vectors) == 1:
            return vectors[0]
        self.semantic_last_error = "embedder 未返回单个查询向量"
        return None

    def _touch(self, note: MemoryNote, now: datetime) -> None:
        note.access_count += 1
        note.last_used_at = now

    def _find_duplicate(self, content: str, tags: list[str]) -> MemoryNote | None:
        """在既有记忆中找高度重复、应合并的那一条（>= DEDUP_SIMILARITY 取最高）。

        与 :meth:`_rank_lexical` 同口径复用词法倒排索引：先取"与 content 至少有一个
        公共 token"的候选并集，再只对候选逐条算 :func:`token_similarity`。公共 token
        交集为空当且仅当 ``token_similarity == 0``，而合并阈值 ``DEDUP_SIMILARITY``
        远高于 0，候选之外的记忆本就不可能命中，故结果与全表扫描逐字节一致，只是把
        写入路径的 O(n) 全量打分降为 O(候选数)。content 切不出 token 时回退全扫。
        """
        best: MemoryNote | None = None
        best_sim = 0.0
        index = self._ensure_lex_index()
        content_tokens = _index_tokens(content)
        query_sets = (
            frozenset(tokenize(canonicalize(content, DEFAULT_SYNONYM_GROUPS))[0]),
            frozenset(tokenize(canonicalize(content, DEFAULT_SYNONYM_GROUPS))[1]),
        )
        candidates: list[str] | None = None
        if content_tokens:
            # 规模治理（10k）：先按"共享 token 数"预筛。合并阈值 0.92 意味着
            # 0.4*uni + 0.6*bi >= 0.92，可推出共享 token 数 >= 0.8 * |query_tokens|，
            # 因此阈值取 0.75 只丢不可能的候选，不改变去重结果。
            shared: dict[str, int] = {}
            for token in content_tokens:
                for note_id in index.get(token, ()):
                    shared[note_id] = shared.get(note_id, 0) + 1
            min_shared = max(1, int(0.75 * len(content_tokens)))
            candidates = [
                note_id
                for note_id, count in shared.items()
                if count >= min_shared
            ]
            if len(candidates) > 256:
                candidates = sorted(
                    candidates, key=lambda note_id: shared[note_id], reverse=True
                )[:256]
        for note in self._notes:
            if candidates is not None:
                if note.id not in shared:
                    continue
                if note.id not in candidates:
                    continue
            sim = self._similarity_from_sets(query_sets, self._token_sets(note))
            if sim >= DEDUP_SIMILARITY and sim > best_sim:
                best = note
                best_sim = sim
        return best

    @staticmethod
    def _normalize_tags(tags: list[str] | None) -> list[str]:
        if not tags:
            return []
        cleaned = [str(t).strip() for t in tags if t is not None and str(t).strip()]
        return list(dict.fromkeys(cleaned))

    @staticmethod
    def _normalize_importance(value: Any) -> int:
        try:
            num = int(value)
        except (TypeError, ValueError):
            return 3
        return max(1, min(5, num))

    @staticmethod
    def _note_dict(note: MemoryNote, score: float | None = None) -> dict[str, Any]:
        data = note.to_dict()
        if score is not None:
            data["score"] = score
        return data

    # ---------- 内部：持久化（原子写 + 可选透明加密） ----------

    def _load(self) -> None:
        """读取落盘记忆；失败时按 ``on_corruption`` 处置，绝不静默丢档。

        分类与处置：
        - 文件不存在：全新库（``missing``），可写；
        - 读取失败：``unreadable``；
        - 解密失败：``decrypt_failed``（换对密钥后原文件仍可读，默认隔离备份）；
        - JSON / 结构损坏：``corrupt``；
        - 存储版本高于当前支持：``version_incompatible``（不猜测新格式）。
        三种失败都先发 ``memory_load_failed`` 诊断，再按策略处理；``quarantine``
        先把原始字节复制到 ``<name>.corrupt-<时间戳>`` 备份，再允许空库写入；
        备份动作失败则自动降级只读，原文件分毫不动。解密失败与版本不兼容即便
        选了 ``quarantine`` 也只读——原始档案可能仍然有效，覆盖它等于丢数据。
        """
        self._notes = []
        self.load_state = _empty_load_state()
        self._dirty = False
        self._forgotten_ids.clear()
        self._stored_owner = None
        self._disk_signature = None
        if self.storage_path is None:
            return
        if not self.storage_path.exists():
            self.load_state["status"] = LOAD_MISSING
            # 文件还不存在：之后若被别的实例建出来，同样算外部修改（先合并再写）。
            self._disk_signature = _MISSING_DISK_SIGNATURE
            return

        try:
            before = self.storage_path.stat()
            raw = self.storage_path.read_bytes()
        except OSError as exc:
            self._handle_load_problem(LOAD_UNREADABLE, f"{type(exc).__name__}: {exc}")
            return

        status, detail, payload = self._decode_payload(raw)
        if payload is None:
            self._handle_load_problem(status, detail)
            return

        # 租户归属校验必须发生在解析笔记之前：不匹配时连一条记忆都不能读进来。
        # 两个方向都 fail-closed：
        #   - 声明了 owner 却读到别人的库 = 越权读；
        #   - **没声明 owner 却读到别人声明的库 = 配置漏项**，同样不能读，更不能在
        #     下次保存时把文件里的 owner 抹成 null（那会让归属保护被不可逆降级）。
        stored_owner = payload.get("owner")
        if self._owner is None and stored_owner is not None:
            self._handle_load_problem(
                LOAD_OWNER_MISMATCH,
                f"文件已声明 owner={stored_owner!r}，而本实例未声明 owner；"
                "fail-closed：既不读取也不覆盖（请给 MemoryVault 传对应 owner，"
                "或改用独立路径）",
            )
            return
        if self._owner is not None and stored_owner not in (None, self._owner):
            self._handle_load_problem(
                LOAD_OWNER_MISMATCH,
                f"文件 owner={stored_owner!r} 与声明的 {self._owner!r} 不一致",
            )
            return
        self._stored_owner = stored_owner if isinstance(stored_owner, str) else None

        status, detail, parsed = self._parse_notes(payload)
        if parsed is None:
            self._handle_load_problem(status, detail)
            return
        self._notes = parsed
        self.load_state["status"] = LOAD_OK
        # 用读取前的 stat 作为指纹：若读窗口内被并发替换，下一次保存会检测到
        # 变化并先合并，而不是用旧内容覆盖别人的新增。
        self._disk_signature = (before.st_mtime_ns, before.st_size)

    # ---------- 内部：解密 / 解析（加载与"外部修改后重读"共用同一套分类） ----------

    def _decode_payload(self, raw: bytes) -> tuple[str, str, dict[str, Any] | None]:
        """解密 + JSON 解析 + 版本校验，返回 ``(status, detail, payload)``。

        ``payload`` 为 None 表示失败：``status`` 是分类（见 ``LOAD_*``），``detail`` 是
        给人看的原因。抽成纯函数是为了让"启动加载"和"写前发现外部修改后的重读"用
        **同一套**分类口径，不会两边各判一套。
        """
        if self._cipher is not None:
            try:
                raw = self._cipher.decrypt(raw)
            except Exception as exc:  # noqa: BLE001 - 解密失败要分类而非吞掉
                return LOAD_DECRYPT_FAILED, f"{type(exc).__name__}: {exc}", None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return LOAD_CORRUPT, f"JSON 解析失败: {exc}", None
        if not isinstance(payload, dict):
            return LOAD_CORRUPT, "顶层不是 JSON 对象", None
        version = payload.get("version", STORAGE_VERSION)
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version > STORAGE_VERSION
        ):
            return (
                LOAD_VERSION_INCOMPATIBLE,
                f"存储版本 {version!r} 高于当前支持的 {STORAGE_VERSION}",
                None,
            )
        return LOAD_OK, "", payload

    @staticmethod
    def _parse_notes(
        payload: dict[str, Any],
    ) -> tuple[str, str, list[MemoryNote] | None]:
        """把 ``payload["notes"]`` 解析成笔记列表，返回 ``(status, detail, notes)``。"""
        notes = payload.get("notes", [])
        if not isinstance(notes, list):
            return LOAD_CORRUPT, "notes 不是数组", None
        try:
            parsed = [MemoryNote.from_dict(item) for item in notes]
        except (AttributeError, TypeError, ValueError, KeyError) as exc:
            return LOAD_CORRUPT, f"单条记忆结构非法: {type(exc).__name__}: {exc}", None
        return LOAD_OK, "", parsed

    def _handle_load_problem(self, status: str, detail: str) -> None:
        """按 ``on_corruption`` 处置加载失败，并发出可观测诊断。"""
        assert self.storage_path is not None
        emit(
            self._on_diagnostic,
            DiagnosticEvent(
                kind=str(DiagnosticKind.MEMORY_LOAD_FAILED),
                level=DiagnosticLevel.ERROR,
                message=(
                    f"长期记忆加载失败（{status}）：{self.storage_path}；"
                    f"策略={self._on_corruption}"
                ),
                data={
                    "path": str(self.storage_path),
                    "status": status,
                    "detail": detail,
                    "policy": self._on_corruption,
                },
            ),
        )
        self.load_state.update({"status": status, "detail": detail})
        # 加载失败后本实例的内存状态就是权威版本（原始字节已 quarantine 或只读保护），
        # 后续保存不再做外部修改合并，避免把刚隔离的坏字节又读回来。
        self._disk_signature = None
        if self._on_corruption == "raise":
            raise MemoryVaultLoadError(status, self.storage_path, detail)
        # 解密失败通常是密钥配错、版本不兼容通常是降级运行：这两种的原始档案
        # 本身可能完全有效，覆盖它等于丢数据，所以即便选了 quarantine 也只读。
        if self._on_corruption == "read_only" or status in (
            LOAD_DECRYPT_FAILED,
            LOAD_VERSION_INCOMPATIBLE,
            LOAD_OWNER_MISMATCH,
        ):
            self._set_read_only()
            return
        # 内容损坏 / 不可读：先留原始字节，再允许空库写入；备份失败即转只读。
        quarantined = self._quarantine_original()
        if quarantined is None:
            self._set_read_only()
            return
        self.load_state["quarantined_to"] = quarantined

    def _set_read_only(self) -> None:
        self._read_only = True
        self.load_state["read_only"] = True

    def _quarantine_original(self) -> str | None:
        """把原始文件按字节复制成同目录备份，返回备份路径；失败返回 None。

        用复制而不是改名：原路径保持存在，后续空库写入会原子替换它，而备份
        里的原始字节（可能是密文 / 旧版本结构）完整保留供人工恢复。时间戳带
        微秒，备份名冲突时追加序号，绝不覆盖已有备份。

        备份保留 :data:`QUARANTINE_KEEP` 份（默认 5）：反复崩溃重启不能把目录堆爆。
        """
        assert self.storage_path is not None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        base = f"{self.storage_path.name}.corrupt-{stamp}"
        target = self.storage_path.with_name(base)
        counter = 1
        while target.exists():
            target = self.storage_path.with_name(f"{base}-{counter}")
            counter += 1
        try:
            shutil.copyfile(self.storage_path, target)
        except OSError:
            return None
        self._prune_quarantine()
        return str(target)

    def _prune_quarantine(self) -> None:
        """只保留最近 :data:`QUARANTINE_KEEP` 份本 vault 的损坏备份（尽力而为）。

        只动本目录下 ``<name>.corrupt-*`` 命名的普通文件，不碰任何别的东西；清理失败
        一律忽略——隔离备份的清理绝不该反过来把加载流程弄崩。
        """
        assert self.storage_path is not None
        prefix = f"{self.storage_path.name}.corrupt-"
        try:
            candidates = sorted(
                (
                    path
                    for path in self.storage_path.parent.iterdir()
                    if path.name.startswith(prefix) and path.is_file()
                ),
                key=lambda path: path.name,
            )
        except OSError:  # pragma: no cover - 目录不可读时放弃清理
            return
        excess = len(candidates) - QUARANTINE_KEEP
        if excess <= 0:
            return
        for stale in candidates[:excess]:
            try:
                stale.unlink()
            except OSError:  # pragma: no cover - 删不掉就留着，不影响主流程
                continue

    def _ensure_writable(self) -> None:
        if self._read_only:
            raise MemoryVaultReadOnlyError(
                "长期记忆处于只读降级（加载状态："
                f"{self.load_state['status']}），拒绝写入以避免覆盖未恢复的原始档案："
                f"{self.storage_path}。修复原始文件后重新创建 MemoryVault 即可恢复写入。"
            )

    def _save(self) -> bool:
        """把内存状态原子落盘，返回是否真的写了；只读降级时直接跳过（返回 False）。

        临界区：**"外部修改检测 → 重读合并 → 序列化加密 → 原子写 → 记录指纹"整段**
        在 ``<storage>.lock`` 的写锁内执行（进程内 ``threading.Lock`` + 跨进程
        ``msvcrt.locking`` / ``fcntl.flock``）。不加锁的话，两个进程可能同时读到同一份
        旧文件、各自合并出一版、再先后覆盖——后写者把前者的新增静默吃掉（TOCTOU）。

        **锁获取失败 = 放弃本次磁盘写入**（v0.27 fail-closed）：等满
        :data:`LOCK_TIMEOUT_SECONDS` 仍拿不到锁时，只发 ``memory_lock_timeout``
        诊断、保留内存变更并置 ``dirty``，**绝不无锁执行 read-modify-write**。
        原子替换只能保证这一次写完整，不能保证并发写之间不互相覆盖。后续任何一次
        写入或显式 :meth:`flush` 都会重试。

        Raises:
            OSError: 磁盘满 / 只读 / 文件被占用等写失败（写路径要浮出来让宿主看见）。
            MemoryVaultReadOnlyError: 合并时发现磁盘文件已属于别的租户（拒绝覆盖）。
        """
        if self.storage_path is None:
            return False
        if self._read_only:
            # 只读降级：读路径（如 recall 的 touch）不得落盘改写原文件。
            return False
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.storage_path) as locked:
            if not locked:
                # fail-closed：没拿到锁就绝不做 read-modify-write；内存变更保留
                # （dirty），等下一次写入 / flush 在安全时机重试。
                self._dirty = True
                self.last_write_persisted = False
                self._emit_lock_timeout()
                return False
            if self._external_change_detected():
                self._merge_external()
            payload = {
                "version": STORAGE_VERSION,
                # 绝不把文件里已有的 owner 写成 null：未声明 owner 的实例只是"沿用"归属，
                # 不是替别的租户"取消"归属（否则任何 owner 配置都会把该文件当 legacy 收下）。
                "owner": self._owner if self._owner is not None else self._stored_owner,
                "saved_at": self._clock().isoformat(),
                "notes": [note.to_dict() for note in self._notes],
            }
            raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            if self._cipher is not None:
                raw = self._cipher.encrypt(raw)
            try:
                self._atomic_write(raw)
            except Exception:
                # 写失败必须如实反映在宿主可见的 durability 信号上；异常继续
                # 上抛给写路径，让宿主知道本次变更没有落盘。
                self._dirty = True
                self.last_write_persisted = False
                raise
            # 落盘成功：脏标记清零、墓碑清零（已体现在磁盘上）、重新记录磁盘指纹。
            self._dirty = False
            self.last_write_persisted = True
            self._forgotten_ids.clear()
            self._record_disk_signature()
        return True

    def flush(self) -> bool:
        """在安全时机重试落盘（fail-closed 后由宿主或下一次写入调用）。

        Returns:
            True = 本次真的把内存状态写到了磁盘；False = 只读降级、纯内存库、
            或本次仍未取得锁（内存变更与 ``dirty`` 保留，等待下一次机会）。
        """
        if self.storage_path is None or self._read_only:
            return False
        return self._save_safely(reason="flush")

    def _emit_lock_timeout(self) -> None:
        """未取得写锁、**放弃本次落盘**时的诊断（失败必须可观测，不许静默）。"""
        emit(
            self._on_diagnostic,
            DiagnosticEvent(
                kind=DIAG_MEMORY_LOCK_TIMEOUT,
                level=DiagnosticLevel.ERROR,
                message=(
                    "未能取得长期记忆写锁（等待上限 "
                    f"{LOCK_TIMEOUT_SECONDS:g} 秒或锁文件不可创建）；"
                    "已放弃本次磁盘写入，内存变更保留（dirty），"
                    f"稍后由下一次写入/flush 重试：{self.storage_path}"
                ),
                data={
                    "path": str(self.storage_path),
                    "lock_path": str(self.lock_path),
                    "timeout_seconds": LOCK_TIMEOUT_SECONDS,
                    "locked": False,
                    "persisted": False,
                    "dirty": True,
                },
            ),
        )

    def _save_safely(self, *, reason: str = "") -> bool:
        """尽力落盘：失败只记 ``memory_save_failed`` 诊断 + dirty，绝不向外抛。

        用于读路径（``recall`` / ``arecall`` 的 touch）与启动期容量收敛——这两处的落盘
        失败不该让"读记忆"或"进程启动"失败。下一次成功落盘会写完整状态（全量重写），
        ``dirty`` 自然会清掉。
        """
        if self._read_only or self.storage_path is None:
            return False
        try:
            return self._save()
        except Exception as exc:  # noqa: BLE001 - 保存路径不允许把宿主读/启动打崩
            self._dirty = True
            self.last_write_persisted = False
            emit(
                self._on_diagnostic,
                DiagnosticEvent(
                    kind=DIAG_MEMORY_SAVE_FAILED,
                    level=DiagnosticLevel.ERROR,
                    message=(
                        f"长期记忆落盘失败（{reason or 'save'}）："
                        f"{type(exc).__name__}: {exc}；内存状态已保留，等下次写入重试"
                    ),
                    data={
                        "path": str(self.storage_path),
                        "reason": reason,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                ),
            )
            return False

    # ---------- 内部：外部修改检测与合并 ----------

    def _external_change_detected(self) -> bool:
        """磁盘文件是否在本实例加载 / 上次保存之后被改过（mtime + size 指纹）。"""
        assert self.storage_path is not None
        if self._disk_signature is None:
            return False
        try:
            stat = self.storage_path.stat()
        except OSError:
            # 文件被删：内存状态就是唯一版本，直接重建，不算冲突。
            return False
        return (stat.st_mtime_ns, stat.st_size) != self._disk_signature

    def _record_disk_signature(self) -> None:
        assert self.storage_path is not None
        try:
            stat = self.storage_path.stat()
        except OSError:  # pragma: no cover - 刚写完就 stat 失败属极端情况
            self._disk_signature = None
            return
        self._disk_signature = (stat.st_mtime_ns, stat.st_size)

    def _merge_external(self) -> None:
        """重读被外部修改过的文件，按 note id 合并进内存状态（随后由调用方落盘）。

        合并语义（与任务约定一致）：

        - 本地有、磁盘也有同 id → **本地版本优先**（本地 touched / new 覆盖磁盘）；
        - 磁盘有、本地没有、且不是本地删的 → 保留（别的实例新增的条目不能丢）；
        - 本地 forget / 容量淘汰的 id → 墓碑生效，磁盘版本不得复活；
        - 磁盘文件坏了 / 属于别的租户 → 先 quarantine（或只读降级），绝不静默覆盖。
        """
        assert self.storage_path is not None
        raw = self.storage_path.read_bytes()  # OSError 交给调用方决定吞还是抛
        status, detail, payload = self._decode_payload(raw)
        if payload is None:
            self._quarantine_after_external_change(status, detail)
            return
        stored_owner = payload.get("owner")
        if self._owner is None and stored_owner is not None:
            self._set_read_only()
            raise MemoryVaultReadOnlyError(
                f"磁盘文件在运行期间被改成 owner={stored_owner!r}，而本实例未声明 owner；"
                "为避免覆盖别的租户的档案，已转只读降级"
            )
        if self._owner is not None and stored_owner not in (None, self._owner):
            self._set_read_only()
            raise MemoryVaultReadOnlyError(
                f"磁盘文件在运行期间被改成 owner={stored_owner!r}，与声明的 "
                f"{self._owner!r} 不一致；已转只读降级"
            )
        status, detail, disk_notes = self._parse_notes(payload)
        if disk_notes is None:
            self._quarantine_after_external_change(status, detail)
            return
        self._notes = self._merge_notes(disk_notes)
        self._rebuild_ids()
        self._note_tokens.clear()
        self._lex_index = None
        live_ids = {note.id for note in self._notes}
        self._vectors = {
            note_id: vector
            for note_id, vector in self._vectors.items()
            if note_id in live_ids
        }
        if self._max_notes is not None:
            self._enforce_cap()
        emit(
            self._on_diagnostic,
            DiagnosticEvent(
                kind=DIAG_MEMORY_DISK_MODIFIED,
                level=DiagnosticLevel.WARNING,
                message=(
                    "检测到长期记忆文件被外部修改，已按 id 合并后继续保存："
                    f"{self.storage_path}（合并后 {len(self._notes)} 条）"
                ),
                data={
                    "path": str(self.storage_path),
                    "merged_notes": len(self._notes),
                    "disk_notes": len(disk_notes),
                },
            ),
        )

    def _quarantine_after_external_change(self, status: str, detail: str) -> None:
        """外部文件在运行期间坏掉：损坏内容先 quarantine 再覆盖；"可能仍有效"只读。"""
        assert self.storage_path is not None
        if status in (LOAD_DECRYPT_FAILED, LOAD_VERSION_INCOMPATIBLE):
            # 与启动加载同口径：解密失败 / 版本更高 的原始档案**可能仍然有效**，
            # 覆盖它等于丢数据——只读降级，等宿主换回密钥 / 升级后再处理。
            self._set_read_only()
            raise MemoryVaultReadOnlyError(
                f"磁盘文件在运行期间变为 {status}（{detail}），原始档案可能仍然有效；"
                "为避免用旧格式覆盖，已转只读降级"
            )
        quarantined = self._quarantine_original()
        if quarantined is None:
            self._set_read_only()
            raise MemoryVaultReadOnlyError(
                f"磁盘文件在运行期间变为 {status}（{detail}），且无法隔离备份；"
                "为避免覆盖原始档案，已转只读降级"
            )
        self.load_state.update(
            {
                "status": status,
                "detail": f"运行期间外部修改后不可解析: {detail}",
                "quarantined_to": quarantined,
            }
        )
        emit(
            self._on_diagnostic,
            DiagnosticEvent(
                kind=str(DiagnosticKind.MEMORY_LOAD_FAILED),
                level=DiagnosticLevel.ERROR,
                message=(
                    f"长期记忆文件在运行期间被改成不可解析（{status}），原始字节已隔离到 "
                    f"{quarantined}；将以内存状态覆盖：{self.storage_path}"
                ),
                data={
                    "path": str(self.storage_path),
                    "status": status,
                    "detail": detail,
                    "quarantined_to": quarantined,
                },
            ),
        )
        # 原始字节已留档：本实例内存状态即权威版本，后续保存直接覆盖、不再合并。
        self._disk_signature = None

    def _merge_notes(self, disk_notes: list[MemoryNote]) -> list[MemoryNote]:
        """按 id 合并磁盘笔记与内存笔记：本地优先、磁盘新增保留、墓碑不复活。"""
        local_by_id = {note.id: note for note in self._notes}
        merged: list[MemoryNote] = []
        seen: set[str] = set()
        for disk_note in disk_notes:
            if disk_note.id in self._forgotten_ids:
                continue
            merged.append(local_by_id.get(disk_note.id, disk_note))
            seen.add(disk_note.id)
        for local_note in self._notes:
            if local_note.id in seen or local_note.id in self._forgotten_ids:
                continue
            merged.append(local_note)
            seen.add(local_note.id)
        return merged

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """POSIX 上 fsync 父目录，让 rename 本身也持久（Windows 无此概念，直接跳过）。"""
        if os.name == "nt":
            return
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
        except OSError:  # pragma: no cover - 平台 / 权限不支持时放弃，不影响主流程
            return
        try:
            os.fsync(dir_fd)
        except OSError:  # pragma: no cover
            pass
        finally:
            os.close(dir_fd)

    def _atomic_write(self, raw: bytes) -> None:
        assert self.storage_path is not None
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.storage_path.parent), prefix=".memory-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                # 先落数据再 rename：否则掉电可能留下"名字已换、内容没到"的空/半截文件，
                # 而 quarantine 只能救坏字节、救不回上一版好数据。
                os.fsync(handle.fileno())
            # Windows 上目标文件可能被短暂读句柄/索引器占用；共享冲突属于瞬态，
            # 交有界重试处理；仍失败才上抛，绝不把半截文件当成功。
            replace_with_retry(tmp_name, str(self.storage_path))
            self._fsync_directory(self.storage_path.parent)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass


# 兼容旧导入路径；实际实现已拆到 memory/tools.py。
from yai_core.memory.tools import build_memory_tools  # noqa: E402,F401
