"""长期记忆的 remember / recall 工具构造器。

从 ``memory/vault.py`` 拆出；``vault`` 继续 re-export，旧导入路径保持可用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from yai_core.memory.constants import DEFAULT_RECALL_K, VALID_KINDS
from yai_core.types import ToolSpec

if TYPE_CHECKING:
    from yai_core.memory.vault import MemoryVault


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
            # 容量已满且新条目被当场淘汰时 stored=False：不能让模型以为"记住了"
            # （条目不在库里，重启后也不会回来）。
            "stored": not vault.last_remember_evicted,
            # 是否真的持久化到磁盘：锁超时/写失败时内存里有、磁盘上没有。
            "persisted": vault.last_write_persisted,
            "dirty": vault.dirty,
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
