"""KV TTL 测试：两种 Store 对等 + SQLite schema v1→v2 迁移专项。

全程离线：内存库 / tmp_path 文件库 / 注入假时钟，零网络、零 Key、零 sleep。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from yai_core.memory import InMemoryStore, SqliteStore
from yai_core.types import ChatMessage


class FakeClock:
    """可手动推进的时钟：推进时间而不是 sleep 等真实窗口。"""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def run(coro):
    """同步测试里跑协程的小工具（仓库既有风格，不引入 pytest-asyncio）。"""
    return asyncio.run(coro)


@pytest.fixture
def make_store(tmp_path):
    """返回 (store, clock)；clock 可手动推进。"""
    start = datetime(2026, 1, 1, 12, 0, 0)

    def _make(kind: str):
        clock = FakeClock(start)
        if kind == "memory":
            return InMemoryStore(clock=clock), clock
        return SqliteStore(tmp_path / "yai.db", clock=clock), clock

    return _make


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_kv_ttl_expires_after_window(make_store, kind) -> None:
    store, clock = make_store(kind)
    run(store.put("token", "abc", ttl_seconds=60))
    assert run(store.get("token")) == "abc"
    clock.advance(seconds=60)  # 恰好等于过期时刻：边界保留
    assert run(store.get("token")) == "abc"
    clock.advance(seconds=1)  # 严格超过过期时刻：惰性过期
    assert run(store.get("token")) is None


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_kv_without_ttl_never_expires(make_store, kind) -> None:
    store, clock = make_store(kind)
    run(store.put("k", "v"))
    clock.advance(days=400)
    assert run(store.get("k")) == "v"


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_kv_overwrite_refreshes_ttl(make_store, kind) -> None:
    store, clock = make_store(kind)
    run(store.put("k", "v1", ttl_seconds=60))
    clock.advance(seconds=50)
    run(store.put("k", "v2", ttl_seconds=60))  # 重写：TTL 从此刻重新计 60s
    clock.advance(seconds=50)  # 距首次 100s（v1 已过期），距二次仅 50s
    assert run(store.get("k")) == "v2"


def test_sqlite_v1_to_v2_migration_preserves_kv(tmp_path) -> None:
    db = tmp_path / "old.db"
    # 手工造一个 v1 老库：kv 没有 expires_at 列，user_version=1。
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE kv (
            scope      TEXT NOT NULL DEFAULT 'default',
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
            PRIMARY KEY (scope, key)
        );
        INSERT INTO kv(scope, key, value) VALUES ('default', 'legacy', 'kept');
        PRAGMA user_version = 1;
        """
    )
    conn.commit()
    conn.close()

    clock = FakeClock(datetime(2026, 1, 1, 12, 0, 0))
    store = SqliteStore(db, clock=clock)  # 打开即自动迁移 v1→v2→v3

    # 旧数据原样保留、可读（无 TTL）
    assert run(store.get("legacy")) == "kept"
    # 迁移后可正常写带 TTL 的键并按其过期
    run(store.put("temp", "x", ttl_seconds=10))
    assert run(store.get("temp")) == "x"
    clock.advance(seconds=11)
    assert run(store.get("temp")) is None
    assert run(store.get("legacy")) == "kept"  # 老键不受影响
    store.close()

    # 迁移确实落库：版本=3、kv 含 expires_at 列、history 含 parts 列
    chk = sqlite3.connect(db)
    assert int(chk.execute("PRAGMA user_version").fetchone()[0]) == 3
    cols = [r[1] for r in chk.execute("PRAGMA table_info(kv)")]
    assert "expires_at" in cols
    history_cols = [r[1] for r in chk.execute("PRAGMA table_info(history)")]
    assert "parts" in history_cols
    chk.close()

    # 再次打开为 v3，幂等不报错、数据仍在
    store2 = SqliteStore(db, clock=clock)
    assert run(store2.get("legacy")) == "kept"
    store2.close()


# ---------- v0.26：迁移幂等对账（半迁移 / 丢 user_version 的库都要能自愈） ----------

_V1_SCHEMA_NO_HISTORY = """
CREATE TABLE kv (
    scope      TEXT NOT NULL DEFAULT 'default',
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    PRIMARY KEY (scope, key)
);
INSERT INTO kv(scope, key, value) VALUES ('default', 'legacy', 'kept');
"""

_V1_HISTORY_SCHEMA = """
CREATE TABLE history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL DEFAULT 'default',
    role         TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    tool_calls   TEXT,
    tool_call_id TEXT,
    name         TEXT,
    created_at   TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
);
INSERT INTO history(scope, role, content) VALUES ('default', 'user', '用户数据还在');
"""


def _columns(db, table: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _user_version(db) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def test_partial_v1_to_v2_migration_is_idempotent(tmp_path) -> None:
    """ALTER 已生效但 user_version 没落盘（进程在两条语句之间被杀）后重开必须自愈。"""
    db = tmp_path / "half_migrated.db"
    conn = sqlite3.connect(db)
    conn.executescript(_V1_SCHEMA_NO_HISTORY + "PRAGMA user_version = 1;")
    # 模拟半迁移：DDL 已提交、版本号还停在 1
    conn.execute("ALTER TABLE kv ADD COLUMN expires_at TEXT")
    conn.commit()
    conn.close()

    store = SqliteStore(db)  # 旧实现这里抛 duplicate column name: expires_at（永远打不开）
    try:
        assert run(store.get("legacy")) == "kept"
        run(store.put("after", "v", ttl_seconds=60))
        assert run(store.get("after")) == "v"
    finally:
        store.close()

    assert _user_version(db) == 3
    assert "expires_at" in _columns(db, "kv")
    assert "parts" in _columns(db, "history")  # history 缺失 -> 建表


def test_version0_legacy_tables_are_reconciled(tmp_path) -> None:
    """user_version=0 但表是老结构（.dump 重建会丢 user_version）：按列对账后可用。"""
    db = tmp_path / "lost_version.db"
    conn = sqlite3.connect(db)
    conn.executescript(_V1_SCHEMA_NO_HISTORY + _V1_HISTORY_SCHEMA)  # 不写 user_version
    conn.commit()
    conn.close()
    assert _user_version(db) == 0

    store = SqliteStore(db)  # 旧实现会把版本号置 3 但永远不补列
    try:
        # 旧数据保留、可读
        assert run(store.get("legacy")) == "kept"
        assert [m.content for m in store.history()] == ["用户数据还在"]
        # 新写入也可用（旧实现：table history has no column named parts）
        run(store.append_history(ChatMessage(role="user", content="迁移后的新消息")))
        run(store.put("fresh", "v", ttl_seconds=60))
        assert [m.content for m in store.history()][-1] == "迁移后的新消息"
        assert run(store.get("fresh")) == "v"
    finally:
        store.close()

    assert _user_version(db) == 3
    assert {"parts"} <= _columns(db, "history")
    assert {"expires_at"} <= _columns(db, "kv")
    # 自愈后再开也不会破坏数据
    again = SqliteStore(db)
    try:
        assert run(again.get("legacy")) == "kept"
        assert len(again.history()) == 2
    finally:
        again.close()


def test_v3_database_missing_column_self_heals(tmp_path) -> None:
    """已经标成 v3 却缺列（旧实现的半迁移产物）也要能被对账修好，而不是永远报错。"""
    db = tmp_path / "broken_v3.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            scope        TEXT NOT NULL DEFAULT 'default',
            role         TEXT NOT NULL,
            content      TEXT NOT NULL DEFAULT '',
            tool_calls   TEXT,
            tool_call_id TEXT,
            name         TEXT,
            created_at   TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
        );
        CREATE TABLE kv (
            scope      TEXT NOT NULL DEFAULT 'default',
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
            PRIMARY KEY (scope, key)
        );
        INSERT INTO history(scope, role, content) VALUES ('default', 'user', 'v3 时代的老数据');
        PRAGMA user_version = 3;
        """
    )
    conn.commit()
    conn.close()

    store = SqliteStore(db)  # 版本号说是 v3，但 parts / expires_at 都不在
    try:
        run(store.append_history(ChatMessage(role="user", content="补列后写入")))
        run(store.put("k", "v", ttl_seconds=60))
        assert [m.content for m in store.history()] == ["v3 时代的老数据", "补列后写入"]
        assert run(store.get("k")) == "v"
        assert run(store.get("v3 时代的老数据")) is None  # KV 与历史不串
    finally:
        store.close()

    assert {"parts"} <= _columns(db, "history")
    assert {"expires_at"} <= _columns(db, "kv")
