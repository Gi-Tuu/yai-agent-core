"""SQLite 在线热备份、恢复与崩溃恢复（重开数据不丢）的离线测试。"""

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from yai_core.memory import sqlite_store
from yai_core.memory.sqlite_store import SqliteStore
from yai_core.types import ChatMessage


def _seed(store: SqliteStore) -> None:
    asyncio.run(store.append_history(ChatMessage(role="user", content="记得我喜欢黑咖啡")))
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


@pytest.mark.parametrize(
    "payload",
    [None, b"", b"not a sqlite database", b" " * 4096],
    ids=["missing", "zero-bytes", "corrupt", "whitespace"],
)
def test_restore_invalid_source_preserves_files(tmp_path: Path, payload: bytes | None) -> None:
    source = tmp_path / "broken.db"
    if payload is not None:
        source.write_bytes(payload)
    target = tmp_path / "out.db"
    old = SqliteStore(target)
    _seed(old)
    old.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    with pytest.raises((OSError, sqlite3.DatabaseError)):
        SqliteStore.restore_to(source, target)

    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    reopened = SqliteStore(target)
    try:
        _assert_seeded(reopened)
    finally:
        reopened.close()


def test_closed_backup_preserves_existing_target(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "source.db")
    store.close()
    target = tmp_path / "out.db"
    old = SqliteStore(target)
    _seed(old)
    old.close()
    before = target.read_bytes()
    with pytest.raises(RuntimeError):
        store.backup_to(target)
    assert target.read_bytes() == before


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


def _files(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


def _closed_seeded(path: Path) -> None:
    store = SqliteStore(path)
    try:
        _seed(store)
    finally:
        store.close()


def test_restore_rejects_schema_less_database(tmp_path: Path) -> None:
    source = tmp_path / "empty.db"
    conn = sqlite3.connect(source)
    conn.execute("VACUUM")  # 非零长度、有合法 SQLite 文件头，但没有 schema。
    conn.close()
    target = tmp_path / "target.db"
    _closed_seeded(target)
    before = _files(tmp_path)
    with pytest.raises(sqlite3.DatabaseError, match="schema"):
        SqliteStore.restore_to(source, target)
    assert _files(tmp_path) == before


def test_restore_rejects_damaged_pages(tmp_path: Path) -> None:
    source = tmp_path / "damaged.db"
    _closed_seeded(source)
    data = bytearray(source.read_bytes())
    # 保留文件头，破坏 sqlite_schema b-tree 页头，必须由 SQLite 校验发现。
    data[100] = 0xFF
    source.write_bytes(data)
    target = tmp_path / "target.db"
    _closed_seeded(target)
    before = _files(tmp_path)
    with pytest.raises(sqlite3.DatabaseError):
        SqliteStore.restore_to(source, target)
    assert _files(tmp_path) == before


@pytest.mark.parametrize("operation", ["backup", "restore"])
@pytest.mark.parametrize("failure", ["copy", "validate", "connect", "replace"])
@pytest.mark.parametrize("existing_target", [False, True])
def test_snapshot_failure_preserves_target_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure: str,
    existing_target: bool,
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    store = SqliteStore(source)
    _seed(store)
    if operation == "restore":
        store.close()
    if existing_target:
        _closed_seeded(target)
    before = _files(tmp_path)
    original_validate = sqlite_store._validate_snapshot
    original_connect = sqlite3.connect

    def fail_copy(src: sqlite3.Connection, staged: Path) -> None:
        staged.write_bytes(b"partial backup")
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(staged) + suffix).write_bytes(b"private partial sidecar")
        raise sqlite3.OperationalError("injected copy failure")

    def fail_validate(conn: sqlite3.Connection) -> None:
        filename = conn.execute("PRAGMA database_list").fetchone()[2]
        if Path(filename).name.startswith(".yai-snapshot-"):
            raise sqlite3.DatabaseError("injected validation failure")
        original_validate(conn)

    def fail_connect(database: str, **kwargs: object) -> sqlite3.Connection:
        if Path(database).name.startswith(".yai-snapshot-"):
            raise sqlite3.OperationalError("injected open failure")
        return original_connect(database, **kwargs)

    def fail_replace(staged: Path, destination: Path) -> None:
        assert staged.parent == destination.parent
        # 发布发生前暂存已完整且是 DELETE 模式（不依赖侧车）。
        conn = original_connect(staged)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            original_validate(conn)
        finally:
            conn.close()
        raise PermissionError("injected replace failure")

    if failure == "copy":
        monkeypatch.setattr(sqlite_store, "_copy_snapshot", fail_copy)
    elif failure == "validate":
        monkeypatch.setattr(sqlite_store, "_validate_snapshot", fail_validate)
    elif failure == "connect":
        monkeypatch.setattr(sqlite_store.sqlite3, "connect", fail_connect)
    else:
        monkeypatch.setattr(sqlite_store.os, "replace", fail_replace)
    try:
        with pytest.raises((sqlite3.DatabaseError, PermissionError), match="injected"):
            if operation == "backup":
                store.backup_to(target)
            else:
                SqliteStore.restore_to(source, target)
        after = _files(tmp_path)
        assert after.keys() == before.keys()
        if operation == "backup":
            # 在线 backup 的 SQLite 读锁会更新源 SHM 的 read marks；并非数据写入。
            before["source.db-shm"] = after["source.db-shm"]
            _assert_seeded(store)
        assert after == before
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["backup", "restore"])
@pytest.mark.parametrize("alias", ["same", "hardlink", "symlink", "case"])
def test_source_alias_is_never_overwritten(tmp_path: Path, operation: str, alias: str) -> None:
    source = tmp_path / "source.db"
    store = SqliteStore(source)
    _seed(store)
    if operation == "restore":
        store.close()
    target = source
    try:
        if alias == "hardlink":
            target = tmp_path / "alias.db"
            target.hardlink_to(source)
        elif alias == "symlink":
            target = tmp_path / "alias.db"
            try:
                target.symlink_to(source)
            except OSError:
                pytest.skip("当前环境无创建符号链接权限")
        elif alias == "case":
            if os.name != "nt":
                pytest.skip("Windows 大小写别名")
            target = tmp_path / "SOURCE.DB"
        before = _files(tmp_path)
        with pytest.raises(ValueError, match="别名"):
            if operation == "backup":
                store.backup_to(target)
            else:
                SqliteStore.restore_to(source, target)
        assert _files(tmp_path) == before
        if operation == "backup":
            _assert_seeded(store)
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["backup", "restore"])
@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_target_sidecars_are_preserved_not_removed(
    tmp_path: Path, operation: str, suffix: str
) -> None:
    source = tmp_path / "source.db"
    store = SqliteStore(source)
    _seed(store)
    if operation == "restore":
        store.close()
    target = tmp_path / "target.db"
    _closed_seeded(target)
    Path(str(target) + suffix).write_bytes(b"unknown sidecar, never delete")
    before = _files(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="WAL/SHM/journal"):
            if operation == "backup":
                store.backup_to(target)
            else:
                SqliteStore.restore_to(source, target)
        assert _files(tmp_path) == before
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_open_wal_target_remains_usable(tmp_path: Path, operation: str) -> None:
    source = tmp_path / "source.db"
    store = SqliteStore(source)
    _seed(store)
    if operation == "restore":
        store.close()
    target = tmp_path / "target.db"
    active = SqliteStore(target)
    _seed(active)
    before = _files(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="WAL/SHM/journal"):
            if operation == "backup":
                store.backup_to(target)
            else:
                SqliteStore.restore_to(source, target)
        assert _files(tmp_path) == before
        _assert_seeded(active)
        asyncio.run(active.put("still", "writable"))
        assert asyncio.run(active.get("still")) == "writable"
    finally:
        active.close()
        store.close()


def test_restore_rejects_live_wal_source_without_touching_it(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    store = SqliteStore(source)
    _seed(store)
    target = tmp_path / "target.db"
    _closed_seeded(target)
    before = _files(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="WAL/SHM/journal"):
            SqliteStore.restore_to(source, target)
        assert _files(tmp_path) == before
        _assert_seeded(store)
    finally:
        store.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_backup_cannot_overwrite_own_sidecar(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / "source.db"
    store = SqliteStore(source)
    _seed(store)
    before = _files(tmp_path)
    try:
        with pytest.raises(ValueError, match="别名"):
            store.backup_to(Path(str(source) + suffix))
        assert _files(tmp_path) == before
        _assert_seeded(store)
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_recheck_sidecars_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    source = tmp_path / "source.db"
    store = SqliteStore(source)
    _seed(store)
    if operation == "restore":
        store.close()
    target = tmp_path / "target.db"
    _closed_seeded(target)
    before = _files(tmp_path)
    copy_snapshot = sqlite_store._copy_snapshot

    def add_sidecar(src: sqlite3.Connection, staged: Path) -> None:
        copy_snapshot(src, staged)
        Path(str(target) + "-wal").write_bytes(b"new external WAL")

    monkeypatch.setattr(sqlite_store, "_copy_snapshot", add_sidecar)
    try:
        with pytest.raises(RuntimeError, match="WAL/SHM/journal"):
            if operation == "backup":
                store.backup_to(target)
            else:
                SqliteStore.restore_to(source, target)
        after = _files(tmp_path)
        if operation == "backup":
            # 源在线连接正常维护读锁；目标主文件和所有侧车仍须精确保留。
            before["source.db-shm"] = after["source.db-shm"]
        assert after == {**before, "target.db-wal": b"new external WAL"}
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_backup_api_failure_closes_temporary_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    original_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    class FailingBackup(sqlite3.Connection):
        def backup(self, target: sqlite3.Connection, **kwargs: object) -> None:
            super().backup(target, **kwargs)
            raise sqlite3.OperationalError("injected backup API failure")

    def connect(database: str, **kwargs: object) -> sqlite3.Connection:
        conn = original_connect(database, factory=FailingBackup, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite_store.sqlite3, "connect", connect)
    source = tmp_path / "source.db"
    store = SqliteStore(source)
    _seed(store)
    if operation == "restore":
        store.close()
    target = tmp_path / "target.db"
    _closed_seeded(target)
    before = target.read_bytes()
    source_before = source.read_bytes()
    names_before = set(_files(tmp_path))
    opened.clear()
    try:
        with pytest.raises(sqlite3.OperationalError, match="backup API failure"):
            if operation == "backup":
                store.backup_to(target)
            else:
                SqliteStore.restore_to(source, target)
        assert target.read_bytes() == before
        assert source.read_bytes() == source_before
        assert set(_files(tmp_path)) == names_before
        assert len(opened) == (1 if operation == "backup" else 2)
        for conn in opened:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                conn.execute("SELECT 1")
    finally:
        store.close()


def test_restore_readonly_uri_and_clean_replacement(tmp_path: Path) -> None:
    source = tmp_path / "备份 # percent%.db"
    _closed_seeded(source)  # 关闭后的 WAL 模式主文件，已 checkpoint 且无侧车。
    target = tmp_path / "target.db"
    conn = sqlite3.connect(target)
    conn.execute("CREATE TABLE obsolete (value TEXT)")
    conn.commit()
    conn.close()
    before = source.read_bytes()
    assert SqliteStore.restore_to(source, target) == target
    assert source.read_bytes() == before
    assert set(_files(tmp_path)) == {source.name, target.name}
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'obsolete'").fetchone()
    finally:
        conn.close()
    reopened = SqliteStore(target)
    try:
        _assert_seeded(reopened)
    finally:
        reopened.close()
