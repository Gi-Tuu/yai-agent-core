"""SQLite 持久化记忆：MemoryStore 契约的第一个持久化实现（opt-in）。

为什么放在 memory/ 而不是 integrations/：sqlite3 是 Python 标准库，
零第三方依赖、零可选 extra，不属于"外部生态集成"，与 InMemoryStore 同层。

设计要点（对应 10-plan-sqlite-memory.md §4 与 10-plan-history-trim-ttl.md §4）：
1. 完整实现 spi/memory.py 的 MemoryStore 契约（五方法），Loop / Core / 三个 host
   示例零改动即可把"便签本"换成"笔记本"；
2. history() 按契约保持同步方法；其余 async 方法内执行同步小事务（单条毫秒级，
   v0.2 不引入 aiosqlite，边界与取舍写进 walkthrough 第 11 章）；
3. scope 实现会话/宿主隔离，默认 "default"，不传参与 InMemoryStore 的单空间行为对齐；
4. PRAGMA user_version 作为 schema 迁移钩子（v0.7 升至 v2：kv 加 expires_at；
   v1 老库打开时自动 ALTER 迁移，历史表不动）；
5. check_same_thread=False 只是放开 sqlite 的线程限制，真正的互斥由 self._lock 保证
   （uvicorn 会把同步函数丢到线程池，多线程可能同时碰一个连接）；
6. v0.3 历史保留策略（opt-in）：max_messages/ttl_seconds/clock 与 InMemoryStore 对等；
   created_at 由注入时钟在 Python 侧写入（与内存实现的时间戳列表同口径，假时钟可测），
   列默认 CURRENT_TIMESTAMP 保留给外部/手工插入；构造时自动 prune 一次覆盖重启场景。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from yai_core.memory.retention import (
    count_cut,
    retention_from_env,
    ttl_cut,
    utc_now,
)
from yai_core.types import ChatMessage, parts_from_references

# 当前代码支持的 schema 版本；_ensure_schema 用它判断"未来版本数据库"并拒绝打开。
_SCHEMA_VERSION = 3

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
    parts        TEXT,
    created_at   TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
);
CREATE INDEX IF NOT EXISTS idx_history_scope_id ON history(scope, id);

CREATE TABLE IF NOT EXISTS kv (
    scope      TEXT NOT NULL DEFAULT 'default',
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    expires_at TEXT,
    PRIMARY KEY (scope, key)
);

PRAGMA user_version = 3;
"""

# v1 → v2 迁移：kv 增加可空 expires_at（NULL = 永不过期）；history 表不动。
# 单独成脚本，ALTER TABLE ADD COLUMN 在老库上幂等性由 user_version=1 分支保证只跑一次。
_MIGRATE_v1_v2_SQL = """
ALTER TABLE kv ADD COLUMN expires_at TEXT;
PRAGMA user_version = 2;
"""

# v2 → v3 迁移：history 增加可空 parts 列（多模态片段的**引用形态**）。
# 只存引用与元数据，**不存图片字节**——历史里不该长期驻留大块二进制。
#
# 迁移**不写成一段裸 SQL**：老库可能根本没有 history 表（手工造过残缺库），
# 那种情况下 ALTER TABLE 会直接炸。两条路分开走：
#   - 表不存在 → 按 v3 建表；
#   - 表在但缺列 → ALTER 补列。
# 两种情况都不丢已有数据。
_MIGRATE_v2_v3_SQL = """
ALTER TABLE history ADD COLUMN parts TEXT;
PRAGMA user_version = 3;
"""

#: v3 的 history 表（建表脚本单独成份，好让迁移路径复用同一份定义）。
_CREATE_HISTORY_V3_SQL = """
CREATE TABLE IF NOT EXISTS history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL DEFAULT 'default',
    role         TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    tool_calls   TEXT,
    tool_call_id TEXT,
    name         TEXT,
    parts        TEXT,
    created_at   TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
);
CREATE INDEX IF NOT EXISTS idx_history_scope_id ON history(scope, id);
"""

_DB_PATH_ENV = "YAI_DB_PATH"

# created_at 的 UTC 文本格式，与 SQLite CURRENT_TIMESTAMP 输出完全一致，可直接字符串比较。
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


class SqliteStore:
    """会话历史 + KV 落 SQLite 的 MemoryStore 实现。

    用法::

        store = SqliteStore("data/yai.db")        # 文件形态（自动建父目录）
        store = SqliteStore(":memory:")           # 内存形态（测试主入口）
        store = SqliteStore.from_env()            # 读 YAI_DB_PATH，未设置返回 None
        # 历史保留策略（opt-in，与 InMemoryStore 对等）：
        store = SqliteStore("data/yai.db", max_messages=200, ttl_seconds=604800)
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        scope: str = "default",
        max_messages: int | None = None,
        ttl_seconds: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._scope = scope
        self._max_messages = max_messages
        self._ttl_seconds = ttl_seconds
        self._clock = clock or utc_now
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
        # 启动清理：覆盖"服务重启后过期/超量历史仍在库"的场景（仅配置了策略时动作）。
        if self._max_messages is not None or self._ttl_seconds is not None:
            self.prune()

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        retention: tuple[int | None, int | None] | None = None,
    ) -> SqliteStore | None:
        """从 YAI_DB_PATH 构造；未设置或为空白时返回 None（装配方据此回退 InMemoryStore）。

        env=None 读真实 os.environ；测试注入假字典，绝不碰真实环境变量。
        retention 可由装配方预先解析后显式传入（serve_example 只解析一次 env，
        避免非法值重复 warning）；缺省在本方法内调用 retention_from_env。
        """
        environ = os.environ if env is None else env
        raw = (environ.get(_DB_PATH_ENV) or "").strip()
        if not raw:
            return None
        if retention is None:
            retention = retention_from_env(environ)
        max_messages, ttl_seconds = retention
        return cls(raw, max_messages=max_messages, ttl_seconds=ttl_seconds)

    def _ensure_schema(self) -> None:
        """建表（版本 0）或核对版本；版本高于本代码支持值时直接报错，拒绝乱开。

        迁移是**逐级**的：一条老库可能连跨两级（v1 → v2 → v3），单次 if/elif 只走一级
        会把库停在中间版本上——下次打开还得再迁一次，而"停在中间"这件事没人会发现。
        """
        with self._lock:
            conn = self._require_conn_locked()
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库 schema 版本 {version} 高于本代码支持的 {_SCHEMA_VERSION}，"
                    "请升级 yai-agent-core"
                )
            if version == 0:
                conn.executescript(_SCHEMA_SQL)
                conn.commit()
                return
            while version < _SCHEMA_VERSION:
                if version == 1:
                    # v1 → v2：kv 补 expires_at，数据原样保留。
                    conn.executescript(_MIGRATE_v1_v2_SQL)
                    version = 2
                elif version == 2:
                    # v2 → v3：history 补 parts 列（多模态片段的引用形态）。
                    self._migrate_v2_to_v3(conn)
                    version = 3
                else:  # pragma: no cover - 未来版本号分支，当前到 3 为止
                    break
                conn.commit()
            # version == _SCHEMA_VERSION：幂等，什么都不做（重复初始化不报错、不丢数据）

    def _migrate_v2_to_v3(self, conn: sqlite3.Connection) -> None:
        """v2 → v3：history 补 parts 列（多模态片段的引用形态）。

        两种老库都要照顾到：

        - **表根本不存在**（手工造过的残缺库）→ 按 v3 建表；
        - **表在但缺列** → ``ALTER TABLE`` 补列。

        两种情况都不丢已有数据；``user_version`` 一律落到 3。
        """
        columns = {row[1] for row in conn.execute("PRAGMA table_info(history)")}
        if not columns:
            conn.executescript(_CREATE_HISTORY_V3_SQL)
        elif "parts" not in columns:
            conn.execute("ALTER TABLE history ADD COLUMN parts TEXT")
        conn.execute("PRAGMA user_version = 3")

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
        # 多模态片段：只存**引用形态**（元数据 + 引用），不落图片字节——
        # 历史里不该长期驻留大块二进制；读回时图片会退化成"模型看不到"的占位。
        raw_parts = getattr(message, "parts", None)
        parts_json = (
            None
            if not raw_parts
            else json.dumps(
                [part.to_reference() for part in raw_parts],
                ensure_ascii=False,
                default=str,
            )
        )
        # created_at 用注入时钟在 Python 侧写：与 InMemoryStore 的时间戳同口径，
        # TTL 才能用假时钟做确定性测试；列默认 CURRENT_TIMESTAMP 仍兜底外部插入。
        created_at = self._clock().strftime(_TS_FORMAT)
        with self._lock:
            conn = self._require_conn_locked()
            conn.execute(
                "INSERT INTO history(scope, role, content, tool_calls, tool_call_id,"
                " name, parts, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._scope,
                    message.role,
                    message.content,
                    tool_calls,
                    message.tool_call_id,
                    message.name,
                    parts_json,
                    created_at,
                ),
            )
            conn.commit()
            self._prune_locked()

    def history(self) -> list[ChatMessage]:
        """同步方法（MemoryStore 契约如此，Loop 启动时直接迭代回放）。

        顺序以自增 id 为准而不是 created_at：同一秒内多条消息时时间戳没有区分度。
        返回全新列表、全新对象，外部改不到内部状态（与 InMemoryStore 返回副本一致）。
        """
        with self._lock:
            conn = self._require_conn_locked()
            rows = conn.execute(
                "SELECT role, content, tool_calls, tool_call_id, name, parts FROM history"
                " WHERE scope = ? ORDER BY id ASC",
                (self._scope,),
            ).fetchall()
        messages: list[ChatMessage] = []
        for row in rows:
            raw_calls = row["tool_calls"]
            tool_calls = json.loads(raw_calls) if raw_calls is not None else None
            raw_parts = row["parts"] if "parts" in row.keys() else None
            parts = None
            if raw_parts:
                try:
                    parts = parts_from_references(json.loads(raw_parts))
                except (TypeError, ValueError):
                    # 片段列损坏不该让整段历史都读不出来：丢掉片段，文本仍在。
                    parts = None
            messages.append(
                ChatMessage(
                    role=row["role"],
                    content=row["content"],
                    tool_calls=tool_calls,
                    tool_call_id=row["tool_call_id"],
                    name=row["name"],
                    parts=parts,
                )
            )
        return messages

    def _prune_locked(self) -> int:
        """按保留策略删除本 scope 的历史前缀，返回删除条数；KV 不动。

        调用方必须已持有 self._lock。统一走"取行 → Python 算轮对齐边界 → 按 id
        删前缀"，条数裁剪与 TTL 共用一条路径（裸 DELETE created_at 会绕过轮对齐，
        可能切散工具调用对）。TTL 判定全部过期时（k == 行数）整 scope 清空。

        规模治理（长期运行的高频写入路径）：每次 append 都会调本方法，绝不能在
        明明无需裁剪时也把整段历史 ``fetchall`` 回 Python。这里先用走 ``(scope,id)``
        索引的 ``COUNT(*)`` / ``MIN(created_at)`` 聚合做快速闸门，只有"条数确超限"
        或"最早一行确已过期"时才取全量行做轮对齐；聚合在 SQLite 引擎内完成，不构造
        任何 Row。快速闸门只排除"必然不裁"的情况，宁可在边界附近多取一次行。
        """
        conn = self._require_conn_locked()
        # 既未设条数上限也未设 TTL：必然无可裁剪，连聚合都省掉。
        if self._max_messages is None and self._ttl_seconds is None:
            return 0
        agg = conn.execute(
            "SELECT COUNT(*), MIN(created_at) FROM history WHERE scope = ?",
            (self._scope,),
        ).fetchone()
        total = int(agg[0] or 0)
        if total == 0:
            return 0
        cutoff_text: str | None = None
        may_need_prune = False
        if self._max_messages is not None and total > self._max_messages:
            # 条数确超限才可能需要按轮对齐裁剪（count_cut 还可能因对不齐轮首而不裁）。
            may_need_prune = True
        if self._ttl_seconds is not None:
            cutoff = self._clock() - timedelta(seconds=self._ttl_seconds)
            cutoff_text = cutoff.strftime(_TS_FORMAT)
            # 最早一行都未过期 → 全行未过期，TTL 必然不裁（MIN 走索引、不取行）。
            earliest = agg[1]
            if earliest is not None and earliest < cutoff_text:
                may_need_prune = True
        if not may_need_prune:
            return 0
        # 确有可能裁剪：才取全量 (id, role, created_at) 做轮对齐边界计算。
        rows = conn.execute(
            "SELECT id, role, created_at FROM history WHERE scope = ? ORDER BY id ASC",
            (self._scope,),
        ).fetchall()
        if not rows:
            return 0
        roles = [r["role"] for r in rows]
        k_count = count_cut(roles, self._max_messages)
        k_ttl = None
        if self._ttl_seconds is not None and cutoff_text is not None:
            # created_at 为 UTC 文本，同格式字符串比较即时间比较；严格 <，等于 cutoff 保留。
            expired = [r["created_at"] < cutoff_text for r in rows]
            k_ttl = ttl_cut(roles, expired)
        cuts = [k for k in (k_count, k_ttl) if k is not None]
        if not cuts:
            return 0
        k = max(cuts)
        if k >= len(rows):
            # TTL 判定全部过期：整 scope 历史清空（KV 保留）。
            cur = conn.execute("DELETE FROM history WHERE scope = ?", (self._scope,))
        else:
            # rows[k] 是保留段首行；删掉它之前（同 scope）的所有行，即整轮前缀。
            first_kept_id = rows[k]["id"]
            cur = conn.execute(
                "DELETE FROM history WHERE scope = ? AND id < ?",
                (self._scope, first_kept_id),
            )
        deleted = cur.rowcount if cur.rowcount is not None else 0
        conn.commit()
        return deleted

    def prune(self) -> int:
        """非 MemoryStore 契约方法：显式触发一次历史裁剪，返回删除条数；KV 不动。"""
        with self._lock:
            return self._prune_locked()

    async def put(
        self, key: str, value: str, ttl_seconds: int | None = None
    ) -> None:
        """KV 写入（UPSERT）：覆盖 value/updated_at；ttl_seconds 非 None 时写过期时刻。"""
        expires_at = (
            (self._clock() + timedelta(seconds=ttl_seconds)).strftime(_TS_FORMAT)
            if ttl_seconds is not None
            else None
        )
        with self._lock:
            conn = self._require_conn_locked()
            conn.execute(
                "INSERT INTO kv(scope, key, value, expires_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value,"
                " updated_at = CURRENT_TIMESTAMP, expires_at = excluded.expires_at",
                (self._scope, key, value, expires_at),
            )
            conn.commit()

    async def get(self, key: str) -> str | None:
        """KV 读取；未命中或已过 TTL 返回 None（等于过期时刻保留，与 InMemoryStore 对齐）。"""
        now_text = self._clock().strftime(_TS_FORMAT)
        with self._lock:
            conn = self._require_conn_locked()
            row = conn.execute(
                "SELECT value FROM kv WHERE scope = ? AND key = ?"
                " AND (expires_at IS NULL OR expires_at >= ?)",
                (self._scope, key, now_text),
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

    # ---------- 在线备份 / 恢复（运维，非 MemoryStore 契约） ----------

    def backup_to(self, dest: str | Path) -> Path:
        """在线一致快照；源可在线，目标必须离线且无 WAL/SHM/journal 侧车。

        在实例锁内用 backup API 写同目录临时库，校验、关闭后再原子替换目标。
        拒绝自备份及源侧车别名，不删除目标或其侧车；失败保留旧目标。
        调用方须保证目标整个操作期间无人打开（侧车检查不是跨进程锁）。
        """
        target = Path(dest).expanduser()
        with self._lock:
            src = self._require_conn_locked()
            filename = src.execute("PRAGMA database_list").fetchone()[2]
            source = Path(filename) if filename else None
            _check_destination(target, source)
            with _staged_database(target) as staged:
                _copy_snapshot(src, staged)
                _check_destination(target, source)
                os.replace(staged, target)
        return target

    @classmethod
    def restore_to(cls, backup: str | Path, dest: str | Path) -> Path:
        """离线恢复；源和目标在整个操作期间均须无人打开或修改。

        只读打开已 checkpoint、无侧车的备份；缺失、空白、损坏源均报错。
        校验源 → 同目录暂存 backup → 校验并关闭所有连接 → 单文件替换。
        有侧车时拒绝操作，绝不先删 WAL/SHM；不提供多文件崩溃原子性，
        也不能靠侧车检查检测所有空闲连接，宿主必须先停服并关闭连接。
        """
        source = Path(backup).expanduser().resolve()
        target = Path(dest).expanduser()
        _check_destination(target, source)
        _check_no_sidecars(source)
        source_stat = source.stat()  # 缺失源直接失败，不交给 SQLite 自动建库。
        if not source.is_file():
            raise ValueError("恢复源必须是普通数据库文件")
        if source_stat.st_nlink > 1:
            raise ValueError("恢复源不能有硬链接别名（无法确定其 WAL 路径）")
        with source.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise sqlite3.DatabaseError("备份为空或不是 SQLite 数据库")
        # mode=ro 不创建缺失源；immutable 防止只读 WAL 连接仍创建/修改 SHM。
        # immutable 会忽略 WAL，因此只允许离线且无任何侧车的单文件备份。
        with _staged_database(target) as staged:
            with closing(
                sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)
            ) as src:
                _validate_snapshot(src)
                _copy_snapshot(src, staged)
            _check_no_sidecars(source)
            _check_destination(target, source)
            os.replace(staged, target)
        return target


_DB_SIDECARS = ("-wal", "-shm", "-journal")


def _db_sidecars(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in _DB_SIDECARS]


def _check_no_sidecars(path: Path) -> None:
    # 连空侧车/悬空符号链接也拒绝；不能假定它们陈旧，更不能自动删除。
    if any(os.path.lexists(p) for p in _db_sidecars(path)):
        raise RuntimeError("数据库存在 WAL/SHM/journal；请先离线恢复并 checkpoint")


def _check_destination(target: Path, source: Path | None) -> None:
    if source is not None:
        for protected in [source, *_db_sidecars(source)]:
            if target.resolve() == protected.resolve() or (
                target.exists() and protected.exists() and target.samefile(protected)
            ):
                raise ValueError("目标不能是源数据库或其侧车的别名")
    if target.is_symlink() or (target.exists() and target.stat().st_nlink > 1):
        raise ValueError("目标不能是符号链接或有硬链接别名")
    if target.exists() and not target.is_file():
        raise ValueError("目标必须是普通数据库文件")
    _check_no_sidecars(target)


def _validate_snapshot(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise sqlite3.DatabaseError("备份完整性检查失败")
    if not conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
        raise sqlite3.DatabaseError("备份为空数据库（没有 schema）")


def _copy_snapshot(src: sqlite3.Connection, staged: Path) -> None:
    with closing(sqlite3.connect(str(staged))) as out:
        src.backup(out)
        # backup 会继承源的 WAL 模式；发布前变成自包含的单文件数据库。
        if out.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise sqlite3.DatabaseError("无法把快照转换为单文件数据库")
        _validate_snapshot(out)
    _check_no_sidecars(staged)


@contextmanager
def _staged_database(target: Path) -> Iterator[Path]:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".yai-snapshot-", suffix=".db", dir=target.parent)
    staged = Path(name)
    try:
        os.close(fd)
        yield staged
    except BaseException:
        # 仅清理本次独占创建的文件；成功 replace 后不再执行可能失败的文件操作。
        for path in [*_db_sidecars(staged), staged]:
            path.unlink(missing_ok=True)
        raise
