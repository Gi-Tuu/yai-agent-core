"""代码工具注册表与生命周期管理（Code Tool Registry）。

代码工具是模型在运行中生成、需要在宿主沙箱（ToolSandbox SPI）里执行的工具。
内核**不内置任何代码执行器**，这里负责：

1. 把代码工具以 ``source="code"`` 注册进 ToolRegistry（handler 为 None，
   执行时由 ToolExecutor 转给宿主沙箱）；
2. 生命周期：默认 48h TTL，**每次被调用都刷新 TTL**，超时未用则回收；
3. 可被宿主后台置为永久保留（白名单化）；
4. 每次创建/回收都通过事件可观测（事件由调用方 AgentLoop 发出）。

**跨任务持久化（可选）**：传入 ``storage_path`` 后，管理器用**标准库**把代码
工具（code / schema / 生命周期元数据）落盘成 JSON：启动时恢复未过期工具，
create / touch / 永久保留 / 回收后原子写回。只用标准库，不引入第三方依赖、
不触碰内核零硬依赖红线；不传 ``storage_path`` 时行为完全等同纯内存（向后兼容）。

安全说明：内核无法判断一段代码"是否超出宿主能力范围"，这是不可判定的，
因此真正的安全边界由宿主沙箱（无网 / 无敏感文件 / 超时 / 资源上限）+
权限闸（创建与首次执行都可要求授权）共同保证，而不是靠内核猜代码语义。
能用组合工具解决的需求应优先走 compose_tool（零沙箱风险）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yai_core.tools.registry import ToolRegistry
from yai_core.types import ToolSpec

#: 代码工具默认存活时长：48 小时；期间每被调用一次就刷新。
DEFAULT_CODE_TTL_SECONDS = 48 * 60 * 60

#: 持久化文件格式版本；未来不兼容变更时升版并做迁移 / 守卫。
STORAGE_VERSION = 1


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
    """管理代码工具的注册、TTL 刷新、永久保留、过期回收与（可选）持久化。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
        default_ttl: int = DEFAULT_CODE_TTL_SECONDS,
        storage_path: Path | str | None = None,
    ) -> None:
        self.registry = registry
        self._clock = clock or _utc_now
        self.default_ttl = default_ttl
        self._storage = Path(storage_path) if storage_path is not None else None
        self._records: dict[str, CodeToolRecord] = {}
        if self._storage is not None:
            # 启动恢复：未过期工具重新注册，过期项启动即清（见 _load）。
            self._load()

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
        self._save()
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
        self._save()
        return True

    # ---------- 永久保留 / 回收 ----------

    def set_permanent(self, name: str, on: bool) -> bool:
        """设置"永久保留"开关；未知工具返回 False，值未变化则不落盘。"""
        rec = self._records.get(name)
        if rec is None:
            return False
        on = bool(on)
        if rec.permanent != on:
            rec.permanent = on
            self._save()
        return True

    def make_permanent(self, name: str) -> bool:
        """后台把某个代码工具固定保留（白名单化）；未知工具返回 False。"""
        return self.set_permanent(name, True)

    def remove(self, name: str) -> bool:
        """立即删除单个代码工具（不论是否过期）：注销注册表、删记录、落盘。

        与 :meth:`expire_stale` 的区别：后者只批量回收已过期项；``remove`` 供
        后台对任意指定工具立即删除。
        """
        rec = self._records.pop(name, None)
        if rec is None:
            return False
        if self.registry.has(name):
            self.registry.unregister(name)
        self._save()
        return True

    def expire_stale(self, now: datetime | None = None) -> list[str]:
        """回收所有过期且非永久的代码工具（从注册表注销），返回被回收的名字。"""
        now = now or self._clock()
        retired: list[str] = []
        for name, rec in list(self._records.items()):
            if rec.is_expired(now):
                if self.registry.has(name):
                    self.registry.unregister(name)
                del self._records[name]
                retired.append(name)
        if retired:
            self._save()
        return retired

    # ---------- 观测 ----------

    def has_record(self, name: str) -> bool:
        return name in self._records

    def records(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """返回所有代码工具的状态快照（供后台/UI 展示与后台保留）。"""
        now = now or self._clock()
        out: list[dict[str, Any]] = []
        for name, rec in self._records.items():
            description = (
                self.registry.get(name).description
                if self.registry.has(name)
                else ""
            )
            out.append(
                {
                    "name": name,
                    "description": description,
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

    # ---------- 持久化（标准库 JSON，可选） ----------

    def _load(self) -> None:
        """启动时从磁盘恢复未过期代码工具；文件缺失 / 损坏一律冷启动容忍。"""
        assert self._storage is not None
        data: Any = {}
        try:
            text = self._storage.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}
        except FileNotFoundError:
            data = {}
        except (OSError, json.JSONDecodeError):
            # 文件损坏不致命：当作空库，随后 _save 正常覆盖。
            data = {}
        rows = data.get("tools") if isinstance(data, dict) else None
        now = self._clock()
        if isinstance(rows, list):
            for row in rows:
                self._restore_row(row, now)
        # 启动即清理：过期 / 非法项不进 _records，重写后文件只含存活工具。
        self._save()

    def _restore_row(self, row: Any, now: datetime) -> None:
        """恢复单条落盘记录；过期、字段不全、重名或时间非法一律跳过。"""
        if not isinstance(row, dict):
            return
        name = str(row.get("name", "")).strip()
        code = str(row.get("code", "")).strip()
        description = str(row.get("description", "")).strip()
        if not name or not code or not description:
            return
        try:
            created = datetime.fromisoformat(str(row["created_at"]))
            last_used = datetime.fromisoformat(str(row["last_used_at"]))
        except (KeyError, ValueError, TypeError):
            return
        ttl = int(row.get("ttl_seconds", self.default_ttl))
        permanent = bool(row.get("permanent", False))
        call_count = int(row.get("call_count", 0))
        input_schema = row.get("input_schema")
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}
        record = CodeToolRecord(
            name=name,
            created_at=created,
            last_used_at=last_used,
            ttl_seconds=ttl,
            permanent=permanent,
            call_count=call_count,
        )
        if record.is_expired(now):
            return  # 过期不恢复（启动回收）。
        if name in self._records or self.registry.has(name):
            return  # 与已注册工具重名（理论不可达），跳过保安全。
        spec = ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=None,
            source="code",
            code=code,
        )
        self.registry.register(spec)
        self._records[name] = record

    def _save(self) -> None:
        """把当前记录（含 code / schema）原子写回；未配置存储则什么都不做。"""
        if self._storage is None:
            return
        rows: list[dict[str, Any]] = []
        for name, rec in self._records.items():
            # 已从注册表注销（如执行期发现过期被 executor 注销）的不落盘。
            if not self.registry.has(name):
                continue
            spec = self.registry.get(name)
            rows.append(
                {
                    "name": name,
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                    "code": spec.code or "",
                    "created_at": rec.created_at.isoformat(),
                    "last_used_at": rec.last_used_at.isoformat(),
                    "ttl_seconds": rec.ttl_seconds,
                    "permanent": rec.permanent,
                    "call_count": rec.call_count,
                }
            )
        payload = {
            "version": STORAGE_VERSION,
            "default_ttl_seconds": self.default_ttl,
            "tools": rows,
        }
        self._storage.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._storage.with_name(self._storage.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._storage)  # 同卷原子替换（Windows 也成立）。
