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
import tempfile
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yai_core.discovery.scoring import token_similarity
from yai_core.discovery.semantic import cosine_similarity
from yai_core.spi.cipher import StorageCipher
from yai_core.spi.embedding import EmbeddingProvider
from yai_core.types import ToolSpec

#: 落盘格式版本，后续结构演进时据此迁移。
STORAGE_VERSION = 1

#: remember 支持的记忆类型。
VALID_KINDS = ("fact", "preference", "profile", "episode", "note")

#: recall 默认返回条数。
DEFAULT_RECALL_K = 5

#: 语义召回门槛，口径与 discovery.semantic.DEFAULT_SCORE_THRESHOLD 一致
#: （本地 bge-m3 实测：完全无关 query 余弦约 0.30~0.32，语义相关约 0.48~0.52）。
#: 记忆返回的是 top-k 多条而非"只选一个"，因此不设工具发现里的 margin 判定。
SEMANTIC_RECALL_THRESHOLD = 0.45

#: 重复记忆判定阈值：新增与既有记忆相关度达到该值视为同一条，走合并而非新增。
DEDUP_SIMILARITY = 0.92


class MemoryNote:
    """一条结构化长期记忆。"""

    def __init__(
        self,
        content: str,
        *,
        kind: str = "note",
        tags: list[str] | None = None,
        importance: int = 3,
        note_id: str | None = None,
        created_at: datetime | None = None,
        last_used_at: datetime | None = None,
        access_count: int = 0,
    ) -> None:
        now = datetime.now(UTC)
        self.id = note_id or uuid.uuid4().hex[:12]
        self.content = content
        self.kind = kind
        self.tags = tags or []
        self.importance = importance
        self.created_at = created_at or now
        self.last_used_at = last_used_at or now
        self.access_count = access_count

    def candidate_text(self) -> str:
        """供检索 / 嵌入使用的文本：正文 + 标签 + 类型的中文提示。"""
        kind_label = {
            "fact": "事实",
            "preference": "偏好",
            "profile": "画像",
            "episode": "经历",
            "note": "备注",
        }.get(self.kind, "备注")
        parts = [self.content]
        if self.tags:
            parts.append(" ".join(self.tags))
        parts.append(kind_label)
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "kind": self.kind,
            "tags": list(self.tags),
            "importance": self.importance,
            "created_at": self.created_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat(),
            "access_count": self.access_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryNote:
        return cls(
            content=data["content"],
            note_id=data.get("id"),
            kind=data.get("kind", "note"),
            tags=list(data.get("tags", [])),
            importance=int(data.get("importance", 3)),
            created_at=(
                datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
            ),
            last_used_at=(
                datetime.fromisoformat(data["last_used_at"]) if data.get("last_used_at") else None
            ),
            access_count=int(data.get("access_count", 0)),
        )


def _default_clock() -> datetime:
    return datetime.now(UTC)


class MemoryVault:
    """结构化长期记忆的存储与检索。

    Args:
        storage_path: 落盘 JSON 路径；None 时纯内存（不落盘）。
        cipher: 可选的落盘静态加密（第九 SPI）；None 时明文 JSON。
        embedder: 可选文本嵌入器（第八 SPI）；None 时 :meth:`arecall` 退化为纯词法。
        clock: 时间源（测试可注入固定时钟）。
    """

    def __init__(
        self,
        *,
        storage_path: Path | str | None = None,
        cipher: StorageCipher | None = None,
        embedder: EmbeddingProvider | None = None,
        clock: Any = _default_clock,
    ) -> None:
        self.storage_path = Path(storage_path) if storage_path is not None else None
        self._cipher = cipher
        self._embedder = embedder
        self._clock = clock
        self._notes: list[MemoryNote] = []
        # note_id -> 向量的懒缓存：新增 / 合并后该 id 缺失，下次 arecall 批量补算。
        self._vectors: dict[str, Sequence[float]] = {}
        # 最近一次语义通道降级原因（None 表示未降级），便于宿主观测与测试。
        self.semantic_last_error: str | None = None
        self._load()

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
        norm_kind = kind if kind in VALID_KINDS else "note"
        norm_tags = self._normalize_tags(tags)
        norm_importance = self._normalize_importance(importance)

        duplicate = self._find_duplicate(text, norm_tags)
        if duplicate is not None:
            merged_tags = list(dict.fromkeys([*duplicate.tags, *norm_tags]))
            duplicate.tags = merged_tags
            duplicate.importance = max(duplicate.importance, norm_importance)
            duplicate.access_count += 1
            duplicate.last_used_at = self._clock()
            # 合并可能改变正文标签 / 重要度，旧向量失效，下次语义召回时重算。
            self._vectors.pop(duplicate.id, None)
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
            merged = note
            did_merge = False

        self._save()
        return merged, did_merge

    def forget(self, note_id: str) -> bool:
        """删除一条记忆；不存在返回 False。"""
        for index, note in enumerate(self._notes):
            if note.id == note_id:
                del self._notes[index]
                self._vectors.pop(note_id, None)
                self._save()
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
        """按词法相关度检索 top-k 记忆（同步、零依赖、不需要 embedder）。"""
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
            self._save()
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
            self._save()
        return [self._note_dict(note, round(relevance, 3)) for note, _, relevance in top]

    # ---------- 状态 ----------

    def status(self) -> dict[str, Any]:
        by_kind = {kind: 0 for kind in VALID_KINDS}
        for note in self._notes:
            by_kind[note.kind] += 1
        recent = [self._note_dict(note) for note in sorted(
            self._notes, key=lambda n: n.last_used_at, reverse=True
        )[:10]]
        return {
            "total": len(self._notes),
            "by_kind": by_kind,
            "semantic_enabled": self._embedder is not None,
            "semantic_last_error": self.semantic_last_error,
            "recent": recent,
        }

    # ---------- 内部：筛选 / 排序 / 向量 ----------

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

    def _rank_lexical(
        self,
        query: str,
        *,
        kinds: list[str] | None,
        tags: list[str] | None,
    ) -> list[tuple[MemoryNote, float]]:
        """词法通道：返回通过 kinds/tags 过滤且词法相关度 > 0 的 (note, sim)。"""
        ranked: list[tuple[MemoryNote, float]] = []
        for note in self._notes:
            if not self._matches_filters(note, kinds, tags):
                continue
            sim = token_similarity(query, note.candidate_text())
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
        best: MemoryNote | None = None
        best_sim = 0.0
        for note in self._notes:
            sim = token_similarity(content, note.candidate_text())
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
        if self.storage_path is None or not self.storage_path.exists():
            return
        try:
            raw = self.storage_path.read_bytes()
            if self._cipher is not None:
                raw = self._cipher.decrypt(raw)
            payload = json.loads(raw.decode("utf-8"))
            notes = payload.get("notes", []) if isinstance(payload, dict) else []
            self._notes = [MemoryNote.from_dict(item) for item in notes]
        except Exception:
            # 损坏 / 解密失败 / 旧格式：冷启动为空，避免一次坏文件导致永久不可用。
            self._notes = []

    def _save(self) -> None:
        if self.storage_path is None:
            return
        payload = {
            "version": STORAGE_VERSION,
            "saved_at": self._clock().isoformat(),
            "notes": [note.to_dict() for note in self._notes],
        }
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if self._cipher is not None:
            raw = self._cipher.encrypt(raw)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(raw)

    def _atomic_write(self, raw: bytes) -> None:
        assert self.storage_path is not None
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.storage_path.parent), prefix=".memory-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
            os.replace(tmp_name, self.storage_path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass


def build_memory_tools(vault: MemoryVault) -> list[ToolSpec]:
    """构造 remember / recall 两个长期记忆工具，交给内核注册为普通工具。"""

    def _remember_handler(
        content: str,
        kind: str = "note",
        tags: list[str] | None = None,
        importance: int = 3,
    ) -> dict[str, Any]:
        note, merged = vault.remember(
            content, kind=kind, tags=tags, importance=importance
        )
        return {
            "id": note.id,
            "merged": merged,
            "kind": note.kind,
            "tags": note.tags,
            "importance": note.importance,
        }

    async def _recall_handler(
        query: str,
        k: int = DEFAULT_RECALL_K,
        kinds: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        # 走异步双通道：配置 embedder 时词法 + 语义，否则等价纯词法。
        return await vault.arecall(query, k=k, kinds=kinds, tags=tags)

    remember_spec = ToolSpec(
        name="remember",
        description=(
            "把一条值得长期保留的用户信息写入结构化长期记忆。"
            "kind 取值：fact(事实)、preference(偏好)、profile(画像)、episode(经历)、note(备注)；"
            "tags 为便于检索的关键词标签；importance 为 1-5 的重要度。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要记住的具体内容（一句话）"},
                "kind": {"type": "string", "enum": list(VALID_KINDS), "default": "note"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "importance": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
            },
            "required": ["content"],
        },
        handler=_remember_handler,
    )

    recall_spec = ToolSpec(
        name="recall",
        description=(
            "按语义 / 关键词相关度检索长期记忆，返回最相关的若干条；"
            "可按 kind 与 tag 过滤。回答涉及用户偏好、历史、画像、过往事实时先检索。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索 query（自然语言）"},
                "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": DEFAULT_RECALL_K},
                "kinds": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(VALID_KINDS)},
                },
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
        },
        handler=_recall_handler,
    )

    return [remember_spec, recall_spec]
