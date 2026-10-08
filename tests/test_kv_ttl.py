"""KV TTL 测试：两种 Store 对等 + SQLite schema v1→v2 迁移专项。

全程离线：内存库 / tmp_path 文件库 / 注入假时钟，零网络、零 Key、零 sleep。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from yai_core.memory import InMemoryStore, SqliteStore


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
    store = SqliteStore(db, clock=clock)  # 打开即自动迁移 v1→v2

    # 旧数据原样保留、可读（无 TTL）
    assert run(store.get("legacy")) == "kept"
    # 迁移后可正常写带 TTL 的键并按其过期
    run(store.put("temp", "x", ttl_seconds=10))
    assert run(store.get("temp")) == "x"
    clock.advance(seconds=11)
    assert run(store.get("temp")) is None
    assert run(store.get("legacy")) == "kept"  # 老键不受影响
    store.close()

    # 迁移确实落库：版本=2、kv 含 expires_at 列
    chk = sqlite3.connect(db)
    assert int(chk.execute("PRAGMA user_version").fetchone()[0]) == 2
    cols = [r[1] for r in chk.execute("PRAGMA table_info(kv)")]
    assert "expires_at" in cols
    chk.close()

    # 再次打开为 v2，幂等不报错、数据仍在
    store2 = SqliteStore(db, clock=clock)
    assert run(store2.get("legacy")) == "kept"
    store2.close()
