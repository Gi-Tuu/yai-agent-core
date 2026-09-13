"""SQLite 持久化记忆：MemoryStore 契约的第一个持久化实现（opt-in）。

为什么放在 memory/ 而不是 integrations/：sqlite3 是 Python 标准库，
零第三方依赖、零可选 extra，不属于"外部生态集成"，与 InMemoryStore 同层。

设计要点（对应 10-plan-sqlite-memory.md §4）：
1. 完整实现 spi/memory.py 的 MemoryStore 契约（五方法），Loop / Core / 三个 host
   示例零改动即可把"便签本"换成"笔记本"；
2. history() 按契约保持同步方法；其余 async 方法内执行同步小事务（单条毫秒级，
   v0.2 不引入 aiosqlite，边界与取舍写进 walkthrough 第 11 章）；
3. scope 实现会话/宿主隔离，默认 "default"，不传参与 InMemoryStore 的单空间行为对齐；
4. PRAGMA user_version 作为 schema 迁移钩子（v0.2 只有 v1）；
5. check_same_thread=False 只是放开 sqlite 的线程限制，真正的互斥由 self._lock 保证
   （uvicorn 会把同步函数丢到线程池，多线程可能同时碰一个连接）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from yai_core.types import ChatMessage

# 当前代码支持的 schema 版本；_ensure_schema 用它判断"未来版本数据库"并拒绝打开。
_SCHEMA_VERSION = 1

# 建表语句整体作为一个脚本执行；PRAGMA user_version 不能参数化，必须写字面量。
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL DEFAULT 'default',
    role         TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    tool_calls   TEXT,
    tool_call_id TEXT,
    name         TEXT,
    created_at   TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
);
CREATE INDEX IF NOT EXISTS idx_history_scope_id ON history(scope, id);

CREATE TABLE IF NOT EXISTS kv (
    scope      TEXT NOT NULL DEFAULT 'default',
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    PRIMARY KEY (scope, key)
);

PRAGMA user_version = 1;
"""

_DB_PATH_ENV = "YAI_DB_PATH"


class SqliteStore:
    """会话历史 + KV 落 SQLite 的 MemoryStore 实现。

    用法::

        store = SqliteStore("data/yai.db")        # 文件形态（自动建父目录）
        store = SqliteStore(":memory:")           # 内存形态（测试主入口）
        store = SqliteStore.from_env()            # 读 YAI_DB_PATH，未设置返回 None
    """

    def __init__(self, path: str | Path = ":memory:", *, scope: str = "default") -> None:
        self._scope = scope
        # 所有 DB 访问（含同步 history()）都在这把锁内，保证多线程下连接安全。
        self._lock = threading.Lock()
        self._is_memory = str(path) == ":memory:"
        if self._is_memory:
            db_path = ":memory:"
        else:
            # expanduser 支持 "~/..."；父目录不存在就建，宿主少写一行初始化代码。
            p = Path(path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            db_path = str(p)
        # check_same_thread=False：允许线程池线程使用连接；互斥交给 self._lock。
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            db_path, check_same_thread=False
        )
        # Row 工厂：读出的行可以用 row["role"] 按列名访问，少出错。
        self._conn.row_factory = sqlite3.Row
        if not self._is_memory:
            # WAL：读写不互斥、崩溃更稳；:memory: 不需要也设不了。
            self._conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL：WAL 下安全且更快（fsync 频率降低）。
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> SqliteStore | None:
        """从 YAI_DB_PATH 构造；未设置或为空白时返回 None（装配方据此回退 InMemoryStore）。

        env=None 读真实 os.environ；测试注入假字典，绝不碰真实环境变量。
        """
        environ = os.environ if env is None else env
        raw = (environ.get(_DB_PATH_ENV) or "").strip()
        if not raw:
            return None
        return cls(raw)

    def _ensure_schema(self) -> None:
        """建表（版本 0）或核对版本；版本高于本代码支持值时直接报错，拒绝乱开。"""
        with self._lock:
            conn = self._require_conn_locked()
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                conn.executescript(_SCHEMA_SQL)
                conn.commit()
            elif version > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库 schema 版本 {version} 高于本代码支持的 {_SCHEMA_VERSION}，"
                    "请升级 yai-agent-core"
                )
            # version == _SCHEMA_VERSION：幂等，什么都不做（重复初始化不报错、不丢数据）

    def _require_conn_locked(self) -> sqlite3.Connection:
        """关闭后任何方法都给统一错误；调用方必须已持有 self._lock。"""
        if self._conn is None:
            raise RuntimeError("SqliteStore 已关闭")
        return self._conn

    async def append_history(self, message: ChatMessage) -> None:
        # tool_calls 是 list[dict]，落库序列化为 JSON 文本；None 才存 NULL。
        # 易错点：空列表 [] 要存成 "[]"，读回仍是 []，不能折叠成 NULL（对等测试红线）。
        tool_calls = (
            None
            if message.tool_calls is None
            else json.dumps(message.tool_calls, ensure_ascii=False, default=str)
        )
        with self._lock:
            conn = self._require_conn_locked()
            conn.execute(
                "INSERT INTO history(scope, role, content, tool_calls, tool_call_id, name)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self._scope,
                    message.role,
                    message.content,
                    tool_calls,
                    message.tool_call_id,
                    message.name,
                ),
            )
            conn.commit()

    def history(self) -> list[ChatMessage]:
        """同步方法（MemoryStore 契约如此，Loop 启动时直接迭代回放）。

        顺序以自增 id 为准而不是 created_at：同一秒内多条消息时时间戳没有区分度。
        返回全新列表、全新对象，外部改不到内部状态（与 InMemoryStore 返回副本一致）。
        """
        with self._lock:
            conn = self._require_conn_locked()
            rows = conn.execute(
                "SELECT role, content, tool_calls, tool_call_id, name FROM history"
                " WHERE scope = ? ORDER BY id ASC",
                (self._scope,),
            ).fetchall()
        messages: list[ChatMessage] = []
        for row in rows:
            raw_calls = row["tool_calls"]
            tool_calls = json.loads(raw_calls) if raw_calls is not None else None
            messages.append(
                ChatMessage(
                    role=row["role"],
                    content=row["content"],
                    tool_calls=tool_calls,
                    tool_call_id=row["tool_call_id"],
                    name=row["name"],
                )
            )
        return messages

    async def put(self, key: str, value: str) -> None:
        """KV 写入（UPSERT）：同 scope 同 key 覆盖并刷新 updated_at。"""
        with self._lock:
            conn = self._require_conn_locked()
            conn.execute(
                "INSERT INTO kv(scope, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value,"
                " updated_at = CURRENT_TIMESTAMP",
                (self._scope, key, value),
            )
            conn.commit()

    async def get(self, key: str) -> str | None:
        """KV 读取；未命中返回 None（不是 KeyError），与 InMemoryStore 对齐。"""
        with self._lock:
            conn = self._require_conn_locked()
            row = conn.execute(
                "SELECT value FROM kv WHERE scope = ? AND key = ?",
                (self._scope, key),
            ).fetchone()
        return None if row is None else row["value"]

    async def clear(self) -> None:
        """清空**当前 scope** 的历史与 KV；其他会话/宿主的数据不动。"""
        with self._lock:
            conn = self._require_conn_locked()
            conn.execute("DELETE FROM history WHERE scope = ?", (self._scope,))
            conn.execute("DELETE FROM kv WHERE scope = ?", (self._scope,))
            conn.commit()

    def close(self) -> None:
        """关闭连接（非 MemoryStore 契约方法，供宿主 lifespan 停机调用，幂等）。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
