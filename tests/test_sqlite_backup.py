"""SQLite 在线热备份、恢复与崩溃恢复（重开数据不丢）的离线测试。"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from yai_core.memory.sqlite_store import SqliteStore
from yai_core.types import ChatMessage


def _seed(store: SqliteStore) -> None:
    asyncio.run(
        store.append_history(ChatMessage(role="user", content="记得我喜欢黑咖啡"))
    )
    asyncio.run(store.put("drink", "americano"))


def _assert_seeded(store: SqliteStore) -> None:
    msgs = store.history()
    assert len(msgs) == 1
    assert msgs[0].content == "记得我喜欢黑咖啡"
    assert asyncio.run(store.get("drink")) == "americano"


def test_file_db_survives_close_and_reopen(tmp_path: Path) -> None:
    db = tmp_path / "yai.db"
    store = SqliteStore(db)
    _seed(store)
    store.close()

    # 模拟崩溃 / 重启：用同一文件重新打开，历史与 KV 必须都在。
    reopened = SqliteStore(db)
    _assert_seeded(reopened)
    reopened.close()


def test_online_backup_is_openable_consistent_copy(tmp_path: Path) -> None:
    db = tmp_path / "yai.db"
    store = SqliteStore(db)
    _seed(store)

    bak = tmp_path / "backups" / "snapshot.db"
    returned = store.backup_to(bak)
    assert returned == bak
    assert bak.exists()

    restored = SqliteStore(bak)
    _assert_seeded(restored)
    restored.close()
    store.close()


def test_memory_db_can_backup_to_file(tmp_path: Path) -> None:
    store = SqliteStore(":memory:")
    _seed(store)
    bak = tmp_path / "mem_snapshot.db"
    store.backup_to(bak)

    opened = SqliteStore(bak)
    _assert_seeded(opened)
    opened.close()


def test_backup_after_close_raises(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "yai.db")
    store.close()
    with pytest.raises(RuntimeError):
        store.backup_to(tmp_path / "x.db")


def test_restore_to_creates_openable_db(tmp_path: Path) -> None:
    db = tmp_path / "yai.db"
    store = SqliteStore(db)
    _seed(store)
    bak = store.backup_to(tmp_path / "snap.db")
    store.close()

    target = tmp_path / "restored" / "yai.db"
    SqliteStore.restore_to(bak, target)

    opened = SqliteStore(target)
    _assert_seeded(opened)
    opened.close()


def test_restore_corrupt_backup_raises(tmp_path: Path) -> None:
    bad = tmp_path / "broken.db"
    bad.write_text("this is definitely not a sqlite database", encoding="utf-8")
    with pytest.raises(sqlite3.DatabaseError):
        SqliteStore.restore_to(bad, tmp_path / "out.db")


def test_backup_overwrites_stale_target(tmp_path: Path) -> None:
    db = tmp_path / "yai.db"
    store = SqliteStore(db)
    _seed(store)

    target = tmp_path / "snap.db"
    # 先写一份"旧"备份（内容不同），再次备份应干净覆盖而非残留旧表/旧数据。
    SqliteStore(target).close()
    store.backup_to(target)

    opened = SqliteStore(target)
    _assert_seeded(opened)
    opened.close()
    store.close()
