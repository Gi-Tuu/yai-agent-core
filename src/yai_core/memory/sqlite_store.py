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
   v1 老库打开时自动 ALTER 迁移，历史表不动）；迁移实现成**幂等对账**——缺表建表、
   缺列补列、最后在**同一个事务**里置版本号。所以"DDL 已生效但版本号没落"的中间态、
   被外部工具丢掉 user_version 的库、版本已是 v3 却缺列的坏状态都能自愈，不会卡在
   "每次打开都报 duplicate column / no such column"；
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from yai_core.diagnostics import (
    DiagnosticEvent,
    DiagnosticLevel,
    DiagnosticSink,
    emit,
)
from yai_core.memory.retention import (
    count_cut,
    retention_from_env,
    ttl_cut,
    utc_now,
)
from yai_core.types import ChatMessage, parts_from_references

# 当前代码支持的 schema 版本；_ensure_schema 用它判断"未来版本数据库"并拒绝打开。
_SCHEMA_VERSION = 3

#: 自定义诊断种类：存储层的静默兜底 / 维护动作（空库备份、坏行降级、迁移失败）
#: 要能被运维看见。``diagnostics.DiagnosticKind`` 是内核内置枚举，这里按其文档允许的
#: "宿主/实现自定义字符串"用法使用，不改 diagnostics 模块本身。
_DIAG_STORE_PROBLEM = "sqlite_store_problem"

#: 0 字节主库改名备份的时间戳格式（带微秒，天然不重名）。
_EMPTY_DB_STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"

# 建表语句**拆成可单独 execute 的语句**：迁移必须跑在一个显式事务里，而
# ``executescript`` 会先隐式 COMMIT，会把"DDL 与版本号同事务提交"的保证打掉。
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
)
"""

_CREATE_HISTORY_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_history_scope_id ON history(scope, id)"
)

_CREATE_KV_V3_SQL = """
CREATE TABLE IF NOT EXISTS kv (
    scope      TEXT NOT NULL DEFAULT 'default',
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    expires_at TEXT,
    PRIMARY KEY (scope, key)
)
"""

#: 各表在 v3 必须具备的可选列：缺了才 ALTER（幂等对账）。正常 v3 库只做只读探测。
#: 表名/列名都来自这里的字面量常量，不拼任何外部输入。
_OPTIONAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "history": ("parts",),
    "kv": ("expires_at",),
}

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
        on_diagnostic: DiagnosticSink | None = None,
    ) -> None:
        self._scope = scope
        self._max_messages = max_messages
        self._ttl_seconds = ttl_seconds
        self._clock = clock or utc_now
        self._on_diagnostic = on_diagnostic
        # 所有 DB 访问（含同步 history()）都在这把锁内，保证多线程下连接安全。
        self._lock = threading.Lock()
        self._is_memory = str(path) == ":memory:"
        self._db_path = ":memory:"
        if not self._is_memory:
            # expanduser 支持 "~/..."；父目录不存在就建，宿主少写一行初始化代码。
            p = Path(path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            # 0 字节主库先留证据再初始化（详见方法说明），绝不静默当全新库吞掉。
            self._set_aside_empty_db(p)
            self._db_path = str(p)
        # check_same_thread=False：允许线程池线程使用连接；互斥交给 self._lock。
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self._db_path, check_same_thread=False
        )
        # Row 工厂：读出的行可以用 row["role"] 按列名访问，少出错。
        self._conn.row_factory = sqlite3.Row
        try:
            if not self._is_memory:
                # WAL：读写不互斥、崩溃更稳；:memory: 不需要也设不了。
                self._conn.execute("PRAGMA journal_mode=WAL")
                # NORMAL：WAL 下安全且更快（fsync 频率降低）。
                self._conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema()
            # 启动清理：覆盖"服务重启后过期/超量历史仍在库"的场景（仅配置了策略时动作）。
            if self._max_messages is not None or self._ttl_seconds is not None:
                self.prune()
        except BaseException:
            # 构造失败必须把连接关掉：否则 Windows 上库文件仍被占用，宿主连
            # "搬迁 / 回滚数据库文件"都做不了（schema 版本过高正是这种场景）。
            self._close_quietly()
            raise

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        retention: tuple[int | None, int | None] | None = None,
        on_diagnostic: DiagnosticSink | None = None,
    ) -> SqliteStore | None:
        """从 YAI_DB_PATH 构造；未设置或为空白时返回 None（装配方据此回退 InMemoryStore）。

        env=None 读真实 os.environ；测试注入假字典，绝不碰真实环境变量。
        retention 可由装配方预先解析后显式传入（serve_example 只解析一次 env，
        避免非法值重复 warning）；缺省在本方法内调用 retention_from_env。
        ``on_diagnostic`` 透传给实例（存储层的静默兜底动作要能被运维看见）。
        """
        environ = os.environ if env is None else env
        raw = (environ.get(_DB_PATH_ENV) or "").strip()
        if not raw:
            return None
        if retention is None:
            retention = retention_from_env(environ)
        max_messages, ttl_seconds = retention
        return cls(
            raw,
            max_messages=max_messages,
            ttl_seconds=ttl_seconds,
            on_diagnostic=on_diagnostic,
        )

    def _db_label(self) -> str:
        return self._db_path

    def _set_aside_empty_db(self, path: Path) -> None:
        """0 字节主库先改名备份再初始化（SQLite 会把空文件当成全新库）。

        0 字节文件是"库被截断 / 拷贝中断 / 被 touch"的典型残留；SQLite 会静默把它当
        全新空库，表现为"记忆正常但为空"。这里先留证据（``<name>.empty-<ts>``）再让
        SQLite 建新库；若还伴随 WAL/SHM/journal 侧车，说明可能仍有未 checkpoint 的数据，
        **直接拒绝初始化**，交给运维离线恢复（宁可起不来，也不把待恢复的数据踩掉）。
        """
        try:
            stat = path.stat()
        except OSError:
            return
        if not path.is_file() or stat.st_size != 0:
            return
        sidecars = [sidecar for sidecar in _db_sidecars(path) if os.path.lexists(sidecar)]
        if sidecars:
            raise RuntimeError(
                f"数据库主文件 {path} 为 0 字节但存在侧车 "
                f"{[sidecar.name for sidecar in sidecars]}；可能仍有未 checkpoint 的数据，"
                "拒绝自动初始化，请先离线恢复"
            )
        stamp = datetime.now(UTC).strftime(_EMPTY_DB_STAMP_FORMAT)
        base = f"{path.name}.empty-{stamp}"
        target = path.with_name(base)
        counter = 1
        while os.path.lexists(target):  # pragma: no cover - 微秒级时间戳实际不会撞名
            target = path.with_name(f"{base}-{counter}")
            counter += 1
        try:
            os.replace(path, target)
        except OSError as exc:
            raise RuntimeError(
                f"数据库主文件 {path} 为 0 字节，且无法备份为 {target.name}"
                f"（{type(exc).__name__}: {exc}）；拒绝静默初始化"
            ) from exc
        emit(
            self._on_diagnostic,
            DiagnosticEvent(
                kind=_DIAG_STORE_PROBLEM,
                level=DiagnosticLevel.WARNING,
                message=f"数据库主文件为 0 字节，已备份为 {target.name} 后按新库初始化：{path}",
                data={"path": str(path), "backup": str(target), "problem": "empty_database"},
            ),
        )

    def _ensure_schema(self) -> None:
        """把库对账到 v3：缺表建表、缺列补列、最后置 ``user_version``（同一事务）。

        为什么不是"改一段迁移 SQL 序列"：老库可能连跨两级（v1 → v2 → v3），也可能停在
        "DDL 已生效、``user_version`` 还没落"的中间态（进程在两条语句之间被杀），还可能
        被外部工具（例如 ``sqlite3 old.db .dump | sqlite3 new.db``，会丢 user_version）
        重置成 0。这三种状态用同一套**幂等对账**收口，且 DDL 与版本号同事务提交——绝不
        留下"标着 v3 却缺列"的库（那种库第一条 SQL 就报错，而且迁移分支永不再跑）。

        正常 v3 库只走两次 ``PRAGMA table_info`` 只读探测：不写盘、不拿写锁。
        """
        with self._lock:
            conn = self._require_conn_locked()
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库 schema 版本 {version} 高于本代码支持的 {_SCHEMA_VERSION}，"
                    "请升级 yai-agent-core"
                )
            if version == _SCHEMA_VERSION and self._schema_matches_locked(conn):
                # 幂等：重复初始化不报错、不丢数据、不写盘。
                return
            try:
                with self._schema_transaction(conn):
                    self._create_missing_objects_locked(conn)
                    self._add_missing_columns_locked(conn)
                    # PRAGMA 不能参数化；值来自常量 _SCHEMA_VERSION（int），无注入面。
                    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"数据库 schema 迁移失败（{version} -> {_SCHEMA_VERSION}，"
                    f"{self._db_label()}）：{type(exc).__name__}: {exc}"
                ) from exc

    @contextmanager
    def _schema_transaction(self, conn: sqlite3.Connection) -> Iterator[None]:
        """DDL + 版本号同事务（用 SAVEPOINT，不用 ``executescript``）。

        ``executescript`` 会先隐式 COMMIT，而 sqlite3 模块又会为 DML 自己开隐式事务；
        SAVEPOINT 是唯一能在两种模式下都稳定拿到"要么全生效、要么全回滚"的写法。
        """
        conn.execute("SAVEPOINT yai_schema_migration")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK TO yai_schema_migration")
            conn.execute("RELEASE yai_schema_migration")
            raise
        conn.execute("RELEASE yai_schema_migration")

    @staticmethod
    def _table_columns_locked(conn: sqlite3.Connection, table: str) -> set[str]:
        # table 来自模块内字面量常量（_OPTIONAL_COLUMNS / 建表语句），不拼外部输入。
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    def _schema_matches_locked(self, conn: sqlite3.Connection) -> bool:
        """只读探测：表都在、v3 要求的可选列都在，才算"已经是当前 schema"。"""
        for table, required in _OPTIONAL_COLUMNS.items():
            columns = self._table_columns_locked(conn, table)
            if not columns or not set(required).issubset(columns):
                return False
        return True

    def _create_missing_objects_locked(self, conn: sqlite3.Connection) -> None:
        for sql in (_CREATE_HISTORY_V3_SQL, _CREATE_HISTORY_INDEX_SQL, _CREATE_KV_V3_SQL):
            conn.execute(sql)

    def _add_missing_columns_locked(self, conn: sqlite3.Connection) -> None:
        """缺列才 ALTER：幂等，绝不因为"上次迁移已经加过列"而重复 ADD 报错。"""
        for table, required in _OPTIONAL_COLUMNS.items():
            columns = self._table_columns_locked(conn, table)
            for column in required:
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

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
            tool_calls = None
            if raw_calls is not None:
                try:
                    tool_calls = json.loads(raw_calls)
                except (TypeError, ValueError) as exc:
                    # 单行 JSON 坏不该让整段历史——乃至每个任务的启动回放——都读不出来：
                    # 降级成"没有工具调用"，文本仍可回放，并留一条可观测诊断。
                    # （parts 列同口径：损一列不毁全文。）
                    tool_calls = None
                    emit(
                        self._on_diagnostic,
                        DiagnosticEvent(
                            kind=_DIAG_STORE_PROBLEM,
                            level=DiagnosticLevel.ERROR,
                            message=(
                                "历史记录 tool_calls 列 JSON 损坏，已降级为 None："
                                f"{self._db_label()}"
                            ),
                            data={
                                "table": "history",
                                "column": "tool_calls",
                                "problem": "corrupt_json",
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                        ),
                    )
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
        """非 MemoryStore 契约方法：显式触发一次裁剪，返回历史删除条数。

        过期的 KV 行在这里一并清理（``get`` 也会在读到过期行时顺手删）：内存实现
        ``get`` 直接 pop，SQLite 若只过滤不删，kv 表会随 key 空间单调增长。历史裁剪
        规则不变——``_prune_locked`` 是高频写入路径，不为 KV 清理额外加写事务。
        """
        with self._lock:
            self._purge_expired_kv_locked()
            return self._prune_locked()

    def _purge_expired_kv_locked(self) -> int:
        """删除本 scope 已过期的 KV 行，返回删除条数；调用方必须已持有 self._lock。"""
        conn = self._require_conn_locked()
        now_text = self._clock().strftime(_TS_FORMAT)
        cur = conn.execute(
            "DELETE FROM kv WHERE scope = ? AND expires_at IS NOT NULL AND expires_at < ?",
            (self._scope, now_text),
        )
        deleted = cur.rowcount if cur.rowcount is not None else 0
        if conn.in_transaction:
            # DELETE 即使删 0 行也开了写事务；不提交就会把写锁留在连接上，
            # 让同库的另一个实例（另一个 scope）在后面报 "database is locked"。
            conn.commit()
        return deleted

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
        """KV 读取；未命中或已过 TTL 返回 None（等于过期时刻保留，与 InMemoryStore 对齐）。

        过期的行在**读到它时顺手删除**（与内存实现的 pop 对齐）：只过滤不删会让 kv 表
        随 key 空间无界增长，长期运行 / 短 TTL key 的宿主迟早把库撑大。
        """
        now_text = self._clock().strftime(_TS_FORMAT)
        with self._lock:
            conn = self._require_conn_locked()
            row = conn.execute(
                "SELECT value, expires_at FROM kv WHERE scope = ? AND key = ?",
                (self._scope, key),
            ).fetchone()
            if row is None:
                return None
            expires_at = row["expires_at"]
            if expires_at is not None and expires_at < now_text:
                # 精确匹配 expires_at，避免把同一 key 上并发刷新过的新值误删。
                conn.execute(
                    "DELETE FROM kv WHERE scope = ? AND key = ? AND expires_at = ?",
                    (self._scope, key, expires_at),
                )
                if conn.in_transaction:
                    # 即使精确匹配删到 0 行也要收尾，别把写锁留在连接上。
                    conn.commit()
                return None
            return row["value"]

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
            self._close_quietly()

    def _close_quietly(self) -> None:
        """关掉连接且不抛：用于 ``close()`` 与"构造失败必须释放文件句柄"两条路径。"""
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - 关闭失败没有补救手段
                pass

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
