"""子员工：由根员工按需派生的短命执行者（内核零改动，纯薄壳编排）。

与"对等多智能体"的本质区别（层级纪律）：

- 单一根员工对用户负责；子员工不直接与用户对话，结果只回流给根；
- 子员工工具白名单 ⊆ 根，权限单调收窄（``deny_all``：白名单内放行、越权直接拒，
  不挂起、不弹窗）；
- 独立小上下文（``InMemoryStore``）、更小迭代预算，不能自行发现新工具；
- 深度 ≤ :data:`MAX_DEPTH`，同时存活 ≤ :data:`MAX_CONCURRENT`，完成即销毁。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yai_core.channels import CollectChannel
from yai_core.memory import InMemoryStore
from yai_core.policy import AllowlistPolicy

#: 最大委派深度：根(depth 0) → 子(depth 1) → 孙(depth 2)，孙不能再委派。
MAX_DEPTH = 2
#: 一次委派同时存活的子员工上限。
MAX_CONCURRENT = 5
#: 子员工默认迭代预算（小于根的 6）。
DEFAULT_WORKER_ITERS = 3

#: 未显式给工具时，按工具名前缀启发式挑"只读"工具作为最小默认授权。
_READ_PREFIXES = (
    "list_", "get_", "search_", "find_", "sum_", "count_",
    "recall_", "read_", "filter_", "daily_",
)


@dataclass
class WorkerResult:
    """子员工回流给根的结构化结果。"""

    goal: str
    ok: bool
    final_text: str
    granted_tools: list[str]
    denied: list[str] = field(default_factory=list)

    def to_observation(self) -> dict:
        """转成喂给根模型的 tool observation（JSON 可序列化）。"""
        return {
            "goal": self.goal,
            "ok": self.ok,
            "granted_tools": self.granted_tools,
            "denied": self.denied,
            "result": self.final_text,
        }


def default_read_tools(all_names: list[str]) -> list[str]:
    """从根的全部工具里，按名字前缀启发式挑只读工具（未显式授权时的兜底）。"""
    return [n for n in all_names if n.startswith(_READ_PREFIXES)]


def _select_shared_specs(
    root_core, tools: list[str]
) -> tuple[list, list[str]]:
    """从根 registry 取白名单内 ToolSpec（共享 handler/客户端）；返回 (specs, 未知名单)。"""
    by_name = {s.name: s for s in root_core.registry.all()}
    unknown = [t for t in tools if t not in by_name]
    granted = [t for t in tools if t in by_name]
    return [by_name[t] for t in granted], unknown


def _type_value(event) -> str:
    t = getattr(event, "type", "")
    return t.value if hasattr(t, "value") else str(t)


def _failed_tools(events) -> list[str]:
    """扫描 tool_result 中 ok=false 的工具名（沙箱/执行失败，作为信息回流）。"""
    failed: list[str] = []
    for ev in events:
        data = getattr(ev, "data", {}) or {}
        if _type_value(ev) == "tool_result" and data.get("ok") is False:
            failed.append(str(data.get("tool", "?")))
    return failed


def _has_error(events) -> bool:
    return any(_type_value(ev) == "error" for ev in events)


def build_worker_core(
    root_core,
    *,
    goal: str,
    tools: list[str] | None = None,
    max_iters: int = DEFAULT_WORKER_ITERS,
    depth: int = 1,
    register_delegate: bool = False,
):
    """构建一个被收窄的子员工 core（只构建、不运行）。"""
    from yai_core import AgentCore

    all_names = [s.name for s in root_core.registry.all()]
    if tools is None:
        tools = default_read_tools(all_names)
    specs, unknown = _select_shared_specs(root_core, tools)
    granted_names = [s.name for s in specs]

    # deny_all：白名单内 ALLOW，其余直接 DENY——子员工不挂起、不弹窗、不直接问用户，
    # 越权信息随结果回流根，由根决定是否经主浮窗向用户申请扩权后重派。
    policy = AllowlistPolicy(granted_names, mode="deny_all")
    worker = AgentCore(
        root_core.model,
        channel=CollectChannel(),
        memory=InMemoryStore(),
        policy=policy,
        llm_router=False,   # 子任务由根分解、目标明确，省一次路由调用
        discovery=None,     # 不能自行发现新工具，能力边界由根授予
        composition=False,  # 最小化：只允许使用授予的工具
        max_iters=max_iters,
    )
    worker.register_tools(specs)
    # 深度收窄：仅当未到最大深度时，子员工才持有 delegate（可派生更窄的孙员工）。
    if register_delegate and depth < MAX_DEPTH:
        from shell.employee.delegate import attach_delegate

        attach_delegate(worker, depth=depth + 1)
    worker._yai_unknown = unknown
    worker._yai_granted = granted_names
    return worker


async def run_worker(
    root_core,
    *,
    goal: str,
    tools: list[str] | None = None,
    max_iters: int = DEFAULT_WORKER_ITERS,
    depth: int = 1,
    register_delegate: bool = False,
) -> WorkerResult:
    """构建并运行一个子员工，返回回流结果；完成即销毁（局部对象被回收）。"""
    worker = build_worker_core(
        root_core,
        goal=goal,
        tools=tools,
        max_iters=max_iters,
        depth=depth,
        register_delegate=register_delegate,
    )
    result = await worker.run(goal)
    final_text = result.final_text.strip()
    failed = _failed_tools(result.events)
    ok = bool(final_text) and not _has_error(result.events)
    return WorkerResult(
        goal=goal,
        ok=ok,
        final_text=final_text,
        granted_tools=worker._yai_granted,
        denied=worker._yai_unknown + failed,
    )
