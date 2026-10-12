"""结构化长期记忆（Memory Vault）：让 Agent 能主动"记住"与"回忆"。

此前内核的记忆只有"线性对话历史 + 简单 KV"（见 ``memory/inmemory.py``）：
模型没有标准入口把跨会话仍有用的稳定信息沉淀下来，也无法在需要时主动回忆
用户偏好、事实、画像或未决事项。本模块补齐这块，且不破坏内核零硬依赖红线：

- :class:`MemoryNote`：一条结构化长期记忆（content + kind + tags + importance
  + 时间与访问计数）；
- :class:`MemoryVault`：管理记忆条目的写入、去重合并、词法检索、状态与
  （可选）JSON 持久化；
- :func:`build_memory_tools`：``remember`` / ``recall`` 两个 native 工具，
  模型在运行中主动"记一条 / 查一下"，走 :class:`ToolExecutor` 统一权限闸，
  和宿主业务工具完全同构。

检索默认复用 ``discovery/scoring.py`` 的纯标准库词法相关度（零依赖、永远
可用、可离线测试）。**内核不内置向量长期记忆**（ROADMAP 明确不做）：更强的
语义召回由宿主经 :class:`EmbeddingProvider` 提供，未来可作为 vault 的可选
scorer 注入，内核无需改动。

持久化（可选）：传入 ``storage_path`` 后用标准库 JSON 原子落盘，跨会话 /
重启保留，文件损坏冷启动容忍；不传则纯内存（向后兼容）。
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yai_core.discovery.scoring import token_similarity
from yai_core.types import ToolSpec

#: 持久化文件格式版本；未来不兼容变更时升版并做迁移 / 守卫。
STORAGE_VERSION = 1

#: 合法记忆类型。
VALID_KINDS = ("fact", "preference", "profile", "episode", "note")
DEFAULT_KIND = "note"

#: kind 的中文检索标签：让中文 query（"用户偏好"）能匹配英文 kind 条目。
_KIND_LABELS: dict[str, str] = {
    "fact": "事实",
    "preference": "偏好 喜好 喜欢",
    "profile": "画像 个人信息 资料",
    "episode": "事件 经历",
    "note": "笔记 备注",
}

DEFAULT_IMPORTANCE = 3
DEFAULT_RECALL_K = 5
#: 判定"同一条记忆"的词法相似度阈值；越严越不容易误合并。
DEDUP_SIMILARITY = 0.92


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clean_tags(tags: Any) -> list[str]:
    """把 tags 归一为去重后的字符串列表；字符串按单个标签处理。"""
    if not tags:
        return []
    if isinstance(tags, str):
        tags = [tags]
    out: list[str] = []
    for tag in tags:
        if tag is None:
            continue
        text = str(tag).strip()
        if text and text not in out:
            out.append(text)
    return out


def _clean_kind_set(kinds: Any) -> set[str]:
    """把 kinds 过滤为合法 kind 集合；非法值忽略。"""
    if not kinds:
        return set()
    if isinstance(kinds, str):
        kinds = [kinds]
    return {
        str(k).strip()
        for k in kinds
        if str(k).strip() in VALID_KINDS
    }


def _clamp_importance(value: Any) -> int:
    """重要度夹到 1-5；非法值回退默认。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = DEFAULT_IMPORTANCE
    return min(5, max(1, number))


@dataclass
class MemoryNote:
    """一条结构化长期记忆。"""

    id: str
    content: str
    kind: str = DEFAULT_KIND
    tags: list[str] = field(default_factory=list)
    importance: int = DEFAULT_IMPORTANCE
    created_at: datetime = field(default_factory=_utc_now)
    last_used_at: datetime = field(default_factory=_utc_now)
    access_count: int = 0

    def candidate_text(self) -> str:
        """供词法检索的候选文本：正文 + 标签 + kind 中文标签。"""
        return " ".join(
            part
            for part in (
                self.content,
                " ".join(self.tags),
                _KIND_LABELS.get(self.kind, ""),
            )
            if part
        )


#: 运行中"主动记一条"的工具名。
REMEMBER_TOOL = "remember"
#: 运行中"主动回忆"的工具名。
RECALL_TOOL = "recall"


class MemoryVault:
    """管理结构化长期记忆的写入、去重、检索、状态与（可选）持久化。"""

    def __init__(
        self,
        *,
        storage_path: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
        dedup_similarity: float = DEDUP_SIMILARITY,
    ) -> None:
        self._storage = Path(storage_path) if storage_path is not None else None
        self._clock = clock or _utc_now
        self._dedup = dedup_similarity
        self._notes: list[MemoryNote] = []
        if self._storage is not None:
            # 启动恢复（见 _load），随后 _save 重写，只留合法条目。
            self._load()

    # ---------- 写入 ----------

    def remember(
        self,
        content: str,
        *,
        kind: str = DEFAULT_KIND,
        tags: Any = None,
        importance: Any = DEFAULT_IMPORTANCE,
    ) -> tuple[MemoryNote, bool]:
        """沉淀一条长期记忆，返回 ``(条目, 是否为合并已有)``。

        - content 为空抛 ValueError；
        - kind 非法时降级为默认 note；
        - 与已有条目高度相似时不重复保存，而是合并 tags、取更高 importance、
          刷新使用时间（``merged=True``）。
        """
        text = (content or "").strip()
        if not text:
            raise ValueError("remember 需要非空 content")
        if kind not in VALID_KINDS:
            kind = DEFAULT_KIND
        clean_tags = _clean_tags(tags)
        level = _clamp_importance(importance)
        now = self._clock()

        existing = self._find_duplicate(text)
        if existing is not None:
            existing.tags = sorted(set(existing.tags) | set(clean_tags))
            existing.importance = max(existing.importance, level)
            existing.last_used_at = now
            existing.access_count += 1
            self._save()
            return existing, True

        note = MemoryNote(
            id=self._new_id(),
            content=text,
            kind=kind,
            tags=clean_tags,
            importance=level,
            created_at=now,
            last_used_at=now,
        )
        self._notes.append(note)
        self._save()
        return note, False

    def _find_duplicate(self, content: str) -> MemoryNote | None:
        """返回与 content 相似度达阈值的最佳已有条目；没有则 None。"""
        best: MemoryNote | None = None
        best_score = self._dedup
        for note in self._notes:
            score = token_similarity(content, note.candidate_text())
            if score >= best_score:
                best, best_score = note, score
        return best

    # ---------- 检索 ----------

    def recall(
        self,
        query: str,
        *,
        k: int = DEFAULT_RECALL_K,
        kinds: Any = None,
        tags: Any = None,
    ) -> list[dict[str, Any]]:
        """按 query 回忆相关长期记忆，返回 top-k 条目字典；命中即刷新使用时间。

        排序以词法相关度为主，重要度给温和加成；相关度为 0 的条目不返回，
        避免塞进无关内容。kinds / tags 做过滤。
        """
        text = (query or "").strip()
        if not text or not self._notes:
            return []
        limit = max(1, int(k))
        kind_set = _clean_kind_set(kinds)
        tag_set = set(_clean_tags(tags))
        now = self._clock()

        scored: list[tuple[float, MemoryNote, float]] = []
        for note in self._notes:
            if kind_set and note.kind not in kind_set:
                continue
            if tag_set and not (tag_set & set(note.tags)):
                continue
            sim = token_similarity(text, note.candidate_text())
            if sim <= 0:
                continue
            # 重要度只做温和加成（0.75~1.0），不改变"相关度为主"的排序方向。
            score = sim * (0.75 + 0.25 * (note.importance / 5))
            scored.append((score, note, sim))

        # 同分时重要度高、创建晚的在前。
        scored.sort(
            key=lambda item: (item[0], item[1].importance, item[1].created_at),
            reverse=True,
        )
        hits = scored[:limit]
        results: list[dict[str, Any]] = []
        for _score, note, sim in hits:
            note.last_used_at = now
            note.access_count += 1
            results.append(self._note_dict(note, sim))
        if hits:
            self._save()
        return results

    # ---------- 删除 / 状态 ----------

    def forget(self, note_id: str) -> bool:
        """立即删除指定 id 的记忆；不存在返回 False。"""
        for index, note in enumerate(self._notes):
            if note.id == note_id:
                del self._notes[index]
                self._save()
                return True
        return False

    def status(self) -> dict[str, Any]:
        """返回总数、各类型计数与最近使用条目（供后台 / UI）。"""
        by_kind = {kind: 0 for kind in VALID_KINDS}
        for note in self._notes:
            by_kind[note.kind] = by_kind.get(note.kind, 0) + 1
        recent = sorted(self._notes, key=lambda n: n.last_used_at, reverse=True)[:5]
        return {
            "total": len(self._notes),
            "by_kind": by_kind,
            "recent": [self._note_dict(note) for n in recent],
        }

    # ---------- 工具序列化 ----------

    @staticmethod
    def _note_dict(note: MemoryNote, sim: float | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": note.id,
            "content": note.content,
            "kind": note.kind,
            "tags": list(note.tags),
            "importance": note.importance,
            "created_at": note.created_at.isoformat(),
        }
        if sim is not None:
            data["score"] = round(float(sim), 4)
        return data

    def _new_id(self) -> str:
        """短随机 + 计数的记忆 id，避免同毫秒碰撞；纯标准库。"""
        return f"mem_{len(self._notes):04d}_{uuid.uuid4().hex[:6]}"

    # ---------- 持久化（标准库 JSON，可选） ----------

    def _load(self) -> None:
        """启动从磁盘恢复；文件缺失 / 损坏一律冷启动容忍。"""
        storage = self._storage
        if storage is None:
            return  # 未配置持久化文件：纯内存模式，无磁盘可恢复。
        data: Any = {}
        try:
            raw = storage.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except FileNotFoundError:
            data = {}
        except (OSError, json.JSONDecodeError):
            # 文件损坏不致命：当作空库，随后 _save 正常覆盖。
            data = {}
        rows = data.get("notes") if isinstance(data, dict) else None
        if isinstance(rows, list):
            for row in rows:
                note = self._restore_note(row)
                if note is not None:
                    self._notes.append(note)
        # 启动即重写：非法 / 脏数据不落盘。
        self._save()

    def _restore_note(self, row: Any) -> MemoryNote | None:
        """恢复单条落盘记录；字段不全、kind 非法、时间非法一律跳过。"""
        if not isinstance(row, dict):
            return None
        note_id = str(row.get("id", "")).strip()
        content = str(row.get("content", "")).strip()
        kind = str(row.get("kind", DEFAULT_KIND))
        if not note_id or not content or kind not in VALID_KINDS:
            return None
        try:
            created = datetime.fromisoformat(str(row["created_at"]))
            last_used = datetime.fromisoformat(str(row["last_used_at"]))
        except (KeyError, ValueError, TypeError):
            return None
        try:
            access = max(0, int(row.get("access_count", 0)))
        except (TypeError, ValueError):
            access = 0
        return MemoryNote(
            id=note_id,
            content=content,
            kind=kind,
            tags=_clean_tags(row.get("tags")),
            importance=_clamp_importance(row.get("importance", DEFAULT_IMPORTANCE)),
            created_at=created,
            last_used_at=last_used,
            access_count=access,
        )

    def _save(self) -> None:
        """把当前记忆原子写回；未配置存储则什么都不做。"""
        if self._storage is None:
            return
        rows = [
            {
                "id": note.id,
                "content": note.content,
                "kind": note.kind,
                "tags": list(note.tags),
                "importance": note.importance,
                "created_at": note.created_at.isoformat(),
                "last_used_at": note.last_used_at.isoformat(),
                "access_count": note.access_count,
            }
            for note in self._notes
        ]
        payload = {"version": STORAGE_VERSION, "notes": rows}
        self._storage.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._storage.with_name(self._storage.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._storage)  # 同卷原子替换（Windows 也成立）。


def build_memory_tools(vault: MemoryVault) -> list[ToolSpec]:
    """构造 ``remember`` / ``recall`` 两个 native 工具，handler 闭包访问 vault。"""
    remember_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "要长期记住的内容，写成语义完整的一句话，例如'用户对芒果过敏'。"
                ),
            },
            "kind": {
                "type": "string",
                "enum": list(VALID_KINDS),
                "description": (
                    "fact 事实 / preference 偏好 / profile 画像 / episode 事件 / note 一般。"
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "便于检索的标签，例如 ['饮食','过敏']。",
            },
            "importance": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "重要度 1-5，默认 3；越稳定、越关键的信息越高。",
            },
        },
        "required": ["content"],
    }
    recall_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要回忆的内容，自然语言描述，例如'用户有什么饮食禁忌'。",
            },
            "k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "返回条数，默认 5。",
            },
            "kinds": {
                "type": "array",
                "items": {"type": "string", "enum": list(VALID_KINDS)},
                "description": "只在指定类型内回忆。",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "只回忆带任一指定标签的条目。",
            },
        },
        "required": ["query"],
    }

    def _remember_handler(content: str, **kwargs: Any) -> dict[str, Any]:
        note, merged = vault.remember(
            content,
            kind=kwargs.get("kind", DEFAULT_KIND),
            tags=kwargs.get("tags"),
            importance=kwargs.get("importance", DEFAULT_IMPORTANCE),
        )
        return {
            "id": note.id,
            "content": note.content,
            "kind": note.kind,
            "tags": list(note.tags),
            "importance": note.importance,
            "already_existed": merged,
        }

    def _recall_handler(query: str, **kwargs: Any) -> dict[str, Any]:
        memories = vault.recall(
            query,
            k=kwargs.get("k", DEFAULT_RECALL_K),
            kinds=kwargs.get("kinds"),
            tags=kwargs.get("tags"),
        )
        return {"query": query, "count": len(memories), "memories": memories}

    return [
        ToolSpec(
            name=REMEMBER_TOOL,
            description=(
                "把跨会话仍有用的稳定信息（用户偏好 / 事实 / 画像 / 长期事项）主动"
                "记下来，之后随时可 recall。只记真正稳定、未来会复用的信息，不要记录"
                "一次性的临时细节；高度相似的内容会自动合并而非重复保存。"
            ),
            input_schema=remember_schema,
            handler=_remember_handler,
            source="native",
        ),
        ToolSpec(
            name=RECALL_TOOL,
            description=(
                "回答前需要过往背景时，按当前问题回忆相关长期记忆（用户偏好、历史事实、"
                "未决事项）。拿不准是否提过就先 recall，再据此回答；没有相关记忆时返回"
                "空列表，此时不要编造。"
            ),
            input_schema=recall_schema,
            handler=_recall_handler,
            source="native",
        ),
    ]
