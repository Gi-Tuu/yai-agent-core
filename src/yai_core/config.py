"""AgentCore 的集中配置对象：默认值、入参校验与可复用配置。

为什么需要它：:class:`~yai_core.core.AgentCore` 的可选参数很多（路由、发现、沙箱、
记忆、压缩、选档、超时、预算……），直接用一长串关键字参数虽然直接，但宿主在多处
实例化 core（多专员 / 多子员工 / 测试 / 部署）时难以复用同一份配置，也缺少统一的
早期校验。:class:`CoreConfig` 把这些**与具体模型实例无关**的选项收敛成一个可命名、
可传递、可校验的数据对象：

- 默认值与 ``AgentCore.__init__`` 的关键字默认值保持一致（不传即等价）；
- :meth:`validate` 在构造早期就拒绝非法取值（迭代次数、预算类型、压缩策略、
  llm_router 取值、超时等），把错误挡在装配阶段而不是运行中途；
- :meth:`AgentCore.from_config` 用一份配置 + 一个模型实例构造 core。

本模块零第三方依赖，且不导入 ``core``（避免循环）；注入对象（channel / policy /
discovery / sandbox 等）只做类型标注，内核不持有它们的具体实现。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from yai_core.diagnostics import DiagnosticSink
from yai_core.kernel import (
    DEFAULT_MODEL_TIMEOUT,
    AdaptiveRouter,
    Budget,
    CompactionPolicy,
)
from yai_core.spi import (
    Channel,
    MemoryStore,
    PermissionPolicy,
    RouteSelector,
    StorageCipher,
    ToolDiscovery,
    ToolSandbox,
)


def _is_positive_int(value: Any) -> bool:
    """严格正整数：bool 不算（True 虽是 int 子类但语义上不是次数配置）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_nonneg_int(value: Any) -> bool:
    """非负整数：bool 不算。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass
class CoreConfig:
    """与模型实例无关的全部 :class:`AgentCore` 可选项；默认值等价于直接传 kwargs。"""

    # 注入对象（SPI）。
    channel: Channel | None = None
    memory: MemoryStore | None = None
    policy: PermissionPolicy | None = None
    router: AdaptiveRouter | None = None
    discovery: ToolDiscovery | None = None
    sandbox: ToolSandbox | None = None
    learning: RouteSelector | None = None
    on_diagnostic: DiagnosticSink | None = None
    on_feedback: Callable[..., None] | None = None
    # 路由 / 能力开关。
    auto_approve_tools: bool = True
    llm_router: bool | Literal["auto"] = False
    composition: bool = False
    # 代码工具与长期记忆。
    code_storage: Path | str | None = None
    long_memory: bool = False
    memory_storage: Path | str | None = None
    memory_cipher: StorageCipher | None = None
    # 循环 / 上下文控制。
    max_iters: int = 6
    max_clarify_rounds: int = 2
    full_schema_budget: int = 24
    context_compaction: bool | CompactionPolicy = False
    # 成本 / 韧性。
    cost_aware: bool = True
    model_timeout: float | None = DEFAULT_MODEL_TIMEOUT
    budget: Budget | None = None

    def validate(self) -> CoreConfig:
        """校验全部取值；非法即抛 ValueError（装配早期失败，而非运行中途）。"""
        if not _is_positive_int(self.max_iters):
            raise ValueError("max_iters 必须是 >=1 的整数")
        if not _is_nonneg_int(self.max_clarify_rounds):
            raise ValueError("max_clarify_rounds 必须是 >=0 的整数")
        if not _is_nonneg_int(self.full_schema_budget):
            raise ValueError("full_schema_budget 必须是 >=0 的整数")
        if self.llm_router not in (True, False, "auto"):
            raise ValueError('llm_router 只接受 True、False 或 "auto"')
        if not isinstance(self.context_compaction, (bool, CompactionPolicy)):
            raise ValueError("context_compaction 只接受 False、True 或 CompactionPolicy")
        if self.budget is not None and not isinstance(self.budget, Budget):
            raise ValueError("budget 必须是 yai_core.Budget 实例或 None")
        if self.model_timeout is not None and (
            isinstance(self.model_timeout, bool) or self.model_timeout <= 0
        ):
            raise ValueError("model_timeout 必须是正数或 None")
        return self

    def as_kwargs(self) -> dict[str, Any]:
        """展开为 ``AgentCore(model, **cfg.as_kwargs())`` 所需的关键字参数字典。"""
        return dataclasses.asdict(self)
