"""代码工具仓库管理：不跑任务时也能读取 / 永久保留 / 删除某专员自建的代码工具。

设计要点：

- **Core 是每任务临时的**：空闲时根本没有 Core 实例，而代码工具的真实状态在持久化
  JSON 里。所以工具管理不能依赖当前 Core，否则空闲时打不开；
- 这里用一个**不接模型、不接沙箱**的轻量 :class:`CodeToolManager` 直接操作文件，
  复用内核同一份持久化格式、原子写与过期判定，shell 不重复存储逻辑；
- 三个函数都是同步小 IO（单个小 JSON，毫秒级），由工作台 Slot 派发到常驻 worker
  loop 内调用，与运行中的 Core 同线程串行，杜绝并发写竞态。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from yai_core import CodeToolManager, ToolRegistry


def _open_manager(path: Path) -> CodeToolManager:
    # storage_path 非空：__init__ 即 _load 恢复未过期工具（过期项启动即清）。
    return CodeToolManager(ToolRegistry(), storage_path=path)


def vault_status(path: Path | str) -> dict[str, Any]:
    """该专员代码工具仓库状态（文件不存在 = 空仓库）。"""
    return _open_manager(Path(path)).status()


def vault_set_permanent(path: Path | str, name: str, on: bool) -> bool:
    """设置某工具的永久保留开关并落盘；未知工具返回 False。"""
    return _open_manager(Path(path)).set_permanent(name, bool(on))


def vault_remove(path: Path | str, name: str) -> bool:
    """立即删除某工具并落盘；未知工具返回 False。"""
    return _open_manager(Path(path)).remove(name)
