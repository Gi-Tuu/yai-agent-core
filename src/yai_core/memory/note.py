"""长期记忆的笔记模型与索引 token 工具。

从 ``memory/vault.py`` 拆出；``vault`` 继续 re-export 这些名字，保持旧导入路径可用。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from yai_core.discovery.scoring import (
    DEFAULT_SYNONYM_GROUPS,
    canonicalize,
    tokenize,
)


def _index_tokens(text: str) -> set[str]:
    """把文本归一后切成供倒排索引使用的 token 集合（unigram ∪ bigram）。

    必须与 :func:`token_similarity` 内部走完全相同的 ``canonicalize → tokenize``
    归一（含同义词组），这样"query 与某条 note 没有任何公共 token"才严格等价于
    ``token_similarity == 0``，倒排预筛才不会漏掉任何本该命中的记忆。CJK bigram
    （两字）与 unigram（拉丁词 / CJK 单字）token 空间天然不相交，可安全放进同一字典。
    """
    unigrams, bigrams = tokenize(canonicalize(text, DEFAULT_SYNONYM_GROUPS))
    return unigrams | bigrams

#: remember 支持的记忆类型。


def _normalize_note_time(value: Any) -> datetime | None:
    """把落盘 ISO 时间统一成 aware UTC；naive 时间按 UTC 解释。

    旧库/注入时钟可能留下 offset-less 时间；直接与 aware 时间排序会抛
    ``TypeError``，让 status()/容量收敛崩溃。统一归一化比拒绝旧数据更兼容。
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
            created_at=_normalize_note_time(data.get("created_at")),
            last_used_at=_normalize_note_time(data.get("last_used_at")),
            access_count=int(data.get("access_count", 0)),
        )


def _default_clock() -> datetime:
    return datetime.now(UTC)
