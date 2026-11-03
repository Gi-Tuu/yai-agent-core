"""Memory SPI 与内核自带实现。

公共导出（v0.26 数据完整性加固）：除三个实现与工具构造器外，还导出长期记忆的异常
类型、笔记类型与加载状态常量——宿主按公开 API 就能 ``except MemoryVaultReadOnlyError``，
或按 ``LOAD_*`` 判断加载分类，不必再摸 ``yai_core.memory.vault`` 的私有路径。
"""

from yai_core.memory.inmemory import InMemoryStore
from yai_core.memory.retention import retention_from_env
from yai_core.memory.sqlite_store import SqliteStore
from yai_core.memory.vault import (
    LOAD_CORRUPT,
    LOAD_DECRYPT_FAILED,
    LOAD_MISSING,
    LOAD_OK,
    LOAD_OWNER_MISMATCH,
    LOAD_UNREADABLE,
    LOAD_VERSION_INCOMPATIBLE,
    MemoryNote,
    MemoryVault,
    MemoryVaultError,
    MemoryVaultLoadError,
    MemoryVaultReadOnlyError,
    build_memory_tools,
)

__all__ = [
    "LOAD_CORRUPT",
    "LOAD_DECRYPT_FAILED",
    "LOAD_MISSING",
    "LOAD_OK",
    "LOAD_OWNER_MISMATCH",
    "LOAD_UNREADABLE",
    "LOAD_VERSION_INCOMPATIBLE",
    "InMemoryStore",
    "MemoryNote",
    "MemoryVault",
    "MemoryVaultError",
    "MemoryVaultLoadError",
    "MemoryVaultReadOnlyError",
    "SqliteStore",
    "build_memory_tools",
    "retention_from_env",
]
