"""代码工具注册表与生命周期管理（Code Tool Registry）。

代码工具是模型在运行中生成、需要在宿主沙箱（ToolSandbox SPI）里执行的工具。
内核**不内置任何代码执行器**，这里只负责：

1. 把代码工具以 ``source="code"`` 注册进 ToolRegistry（handler 为 None，
   执行时由 ToolExecutor 转给宿主沙箱）；
2. 生命周期：默认 48h TTL，**每次被调用都刷新 TTL**，超时未用则回收；
3. 可被宿主后台置为永久保留（白名单化）；
4. 每次创建/回收都通过事件可观测（事件由调用方 AgentLoop 发出）。

安全说明：内核无法判断一段代码"是否超出宿主能力范围"，这是不可判定的，
因此真正的安全边界由宿主沙箱（无网 / 无敏感文件 / 超时 / 资源上限）+
权限闸（创建与首次执行都可要求授权）共同保证，而不是靠内核猜代码语义。
能用组合工具解决的需求应优先走 compose_tool（零沙箱风险）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from yai_core.tools.registry import ToolRegistry
from yai_core.types import ToolSpec

#: 代码工具默认存活时长：48 小时；期间每被调用一次就刷新。
DEFAULT_CODE_TTL_SECONDS = 48 * 60 * 60


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class CodeToolRecord:
    """单个代码工具的生命周期记录。"""

    name: str
    created_at: datetime
    last_used_at: datetime
    ttl_seconds: int
    permanent: bool = False
    call_count: int = 0

    def is_expired(self, now: datetime) -> bool:
        if self.permanent:
            return False
        return (now - self.last_used_at).total_seconds() > self.ttl_seconds


class CodeToolManager:
    """管理代码工具的注册、TTL 刷新、永久保留与过期回收。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
        default_ttl: int = DEFAULT_CODE_TTL_SECONDS,
    ) -> None:
        self.registry = registry
        self._clock = clock or _utc_now
        self.default_ttl = default_ttl
        self._records: dict[str, CodeToolRecord] = {}

    # ---------- 创建 ----------

    def create(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        code: str,
        *,
        ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> ToolSpec:
        """注册一个代码工具；重名或缺必要字段时抛 ValueError。"""
        now = now or self._clock()
        name = (name or "").strip()
        code = (code or "").strip()
        description = (description or "").strip()
        if not name:
            raise ValueError("代码工具必须提供非空 name")
        if not description:
            raise ValueError("代码工具必须提供非空 description")
        if not code:
            raise ValueError("代码工具必须提供非空 code")
        if self.registry.has(name):
            raise ValueError(f"工具名 {name!r} 已存在，不能重复创建")
        spec = ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema or {"type": "object", "properties": {}},
            handler=None,
            source="code",
            code=code,
        )
        self.registry.register(spec)
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        self._records[name] = CodeToolRecord(
            name=name,
            created_at=now,
            last_used_at=now,
            ttl_seconds=ttl,
        )
        return spec

    # ---------- 调用时刷新 ----------

    def is_live(self, name: str, now: datetime | None = None) -> bool:
        now = now or self._clock()
        rec = self._records.get(name)
        return rec is not None and not rec.is_expired(now)

    def touch(self, name: str, now: datetime | None = None) -> bool:
        """工具被调用时刷新 TTL：存活则刷新 last_used_at 并计数 +1，返回 True。"""
        now = now or self._clock()
        rec = self._records.get(name)
        if rec is None or rec.is_expired(now):
            return False
        rec.last_used_at = now
        rec.call_count += 1
        return True

    # ---------- 永久保留 / 回收 ----------

    def make_permanent(self, name: str) -> bool:
        """后台把某个代码工具固定保留（白名单化）；未知工具返回 False。"""
        rec = self._records.get(name)
        if rec is None:
            return False
        rec.permanent = True
        return True

    def expire_stale(self, now: datetime | None = None) -> list[str]:
        """回收所有过期且非永久的代码工具（从注册表注销），返回被回收的名字。"""
        now = now or self._clock()
        retired: list[str] = []
        for name, rec in list(self._records.items()):
            if rec.is_expired(now):
                self.registry.unregister(name)
                del self._records[name]
                retired.append(name)
        return retired

    # ---------- 观测 ----------

    def has_record(self, name: str) -> bool:
        return name in self._records

    def records(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """返回所有代码工具的状态快照（供后台/UI 展示与后台保留）。"""
        now = now or self._clock()
        out: list[dict[str, Any]] = []
        for name, rec in self._records.items():
            out.append(
                {
                    "name": name,
                    "created_at": rec.created_at.isoformat(),
                    "last_used_at": rec.last_used_at.isoformat(),
                    "ttl_seconds": rec.ttl_seconds,
                    "permanent": rec.permanent,
                    "call_count": rec.call_count,
                    "live": not rec.is_expired(now),
                }
            )
        return out

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        records = self.records(now)
        return {
            "total": len(records),
            "live": sum(1 for r in records if r["live"]),
            "permanent": sum(1 for r in records if r["permanent"]),
            "tools": records,
        }
