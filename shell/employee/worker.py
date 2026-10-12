"""子员工：由根员工按需派生的短命执行者（内核零改动，纯薄壳编排）。

与"对等多智能体"的本质区别（层级纪律）：

- 单一根员工对用户负责；子员工不直接与用户对话，结果只回流给根；
- 子员工工具白名单 ⊆ 根，权限单调收窄（``deny_all``：白名单内放行、越权直接拒，
  不挂起、不弹窗）；
- 独立小上下文（``InMemoryStore``）、更小迭代预算，不能自行发现新工具；
- 深度 ≤ :data:`MAX_DEPTH`，同时存活 ≤ :data:`MAX_CONCURRENT`，完成即销毁。
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from yai_core.channels import CollectChannel
from yai_core.kernel.budget import Budget
from yai_core.memory import InMemoryStore
from yai_core.policy import AllowlistPolicy

if TYPE_CHECKING:
    from yai_core import AgentCore

#: 最大委派深度：根(depth 0) → 子(depth 1) → 孙(depth 2)，孙不能再委派。
MAX_DEPTH = 2
#: 一次委派同时存活的子员工上限。
MAX_CONCURRENT = 5
#: 子员工默认迭代预算（小于根的 6）。
DEFAULT_WORKER_ITERS = 3
#: 单个子员工的默认墙钟超时（秒）：超时即取消并回流，不拖垮整次并行委派。
DEFAULT_WORKER_TIMEOUT = 60.0
#: 子员工预算熔断后的终态标识（区别于正常 done / 墙钟 timeout / 运行 error）。
STATUS_BUDGET_EXCEEDED = "budget_exceeded"


def default_budget(max_iters: int) -> Budget:
    """按迭代轮数为子员工推导一份保守的硬预算（默认总有兜底，防止并行烧钱）。

    口径与内核 :class:`~yai_core.kernel.budget.Budget` 对齐：

    - ``max_model_calls``：每轮至多一次模型调用，再留规划 / 收尾两次余量；
    - ``max_tool_calls``：一轮可能并行多个业务工具（内核按"每个工具"计数），
      按每轮最多 4 个放宽，足以覆盖正常任务，又能挡住失控狂调；
    - ``max_tokens``：任务间差异大、部分后端不回 usage，默认不硬限，避免误伤，
      需要时由根经 delegate 入参或宿主显式覆盖。
    """
    return Budget(
        max_model_calls=max_iters + 2,
        max_tool_calls=max_iters * 4,
        max_tokens=None,
    )

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
    #: 子员工已实际发出调用事件的工具名（按顺序），让根/界面看到它"做了什么"。
    #: 注意：内核在工具返回后才经执行器批量 yield tool_call，因此被墙钟超时取消、
    #: 尚未返回的调用不会计入（此时 status=timeout 已表明该子员工未完成）。
    steps: list[str] = field(default_factory=list)
    #: 终态：done（正常完成）/ timeout（墙钟超时）/ error（运行错误）
    # / budget_exceeded（触发预算硬上限，提前熔断）。
    status: str = "done"
    #: 预算用量快照（配置了预算时随 DONE / BUDGET_EXCEEDED 回流），否则为 None。
    budget: dict | None = None

    def to_observation(self) -> dict:
        """转成喂给根模型的 tool observation（JSON 可序列化）。"""
        return {
            "goal": self.goal,
            "ok": self.ok,
            "status": self.status,
            "granted_tools": self.granted_tools,
            "steps": self.steps,
            "denied": self.denied,
            "budget": self.budget,
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


def _assemble_worker(
    root_core,
    *,
    goal: str,
    tools: list[str] | None = None,
    max_iters: int = DEFAULT_WORKER_ITERS,
    depth: int = 1,
    register_delegate: bool = False,
    budget: Budget | None = None,
    supervisor=None,
) -> tuple[AgentCore, list[str], list[str]]:
    """构建被收窄的子员工 core（只构建、不运行）。

    返回 ``(worker, granted_names, unknown)``：授予工具名与请求了但根不具备的工具名。

    ``budget`` 为 None 时按 ``max_iters`` 推导一份保守硬预算（子员工默认总有花费
    兜底）；``supervisor`` 是跨层级共享的委派并发闸，派生孙员工时原样透传，使
    根→子→孙共用同一把信号量。
    """
    from yai_core import AgentCore

    if budget is None:
        budget = default_budget(max_iters)
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
        budget=budget,      # 每个子员工独立硬预算，触顶回流 budget_exceeded 不崩根
    )
    worker.register_tools(specs)
    # 深度收窄：仅当未到最大深度时，子员工才持有 delegate（可派生更窄的孙员工）。
    # supervisor 原样透传，保证多层委派共用同一个全局并发闸。
    if register_delegate and depth < MAX_DEPTH:
        from shell.employee.delegate import attach_delegate

        attach_delegate(worker, depth=depth + 1, supervisor=supervisor)
    return worker, granted_names, unknown


def build_worker_core(root_core, **kwargs) -> AgentCore:
    """构建一个被收窄的子员工 core（只构建、不运行），返回 worker 本身。"""
    worker, _granted, _unknown = _assemble_worker(root_core, **kwargs)
    return worker


async def run_worker(
    root_core,
    *,
    goal: str,
    tools: list[str] | None = None,
    max_iters: int = DEFAULT_WORKER_ITERS,
    depth: int = 1,
    register_delegate: bool = False,
    timeout: float | None = DEFAULT_WORKER_TIMEOUT,
    on_event=None,
    budget: Budget | None = None,
    supervisor=None,
) -> WorkerResult:
    """构建并运行一个子员工，返回回流结果；完成即销毁（局部对象被回收）。

    - 流式事件驱动：实时提炼调用步骤，并通过可选 ``on_event(goal, type, data)``
      把进度旁路给上层（如灵动岛）；回调可为 async，其异常不影响主任务。
    - ``timeout`` 给单个子员工加墙钟上限，超时即取消、``status=timeout``，
      避免一个子任务卡住拖垮整次并行委派。
    - ``budget`` 为 None 时按 ``max_iters`` 推导保守硬预算；触发预算硬上限时
      ``status=budget_exceeded``、已产出文本随结果回流，根可据此缩小任务重派，
      熔断不作为异常穿透、也不拖垮并行 gather。
    - ``supervisor`` 为跨层级共享并发闸，派生孙员工时透传。
    """
    worker, granted_names, unknown = _assemble_worker(
        root_core,
        goal=goal,
        tools=tools,
        max_iters=max_iters,
        depth=depth,
        register_delegate=register_delegate,
        budget=budget,
        supervisor=supervisor,
    )
    steps: list[str] = []
    failed: list[str] = []
    final_text = ""
    status = "done"
    budget_snap: dict | None = None

    async def _drive() -> None:
        nonlocal final_text, status, budget_snap
        async for ev in worker.astream(goal):
            kind = _type_value(ev)
            data = getattr(ev, "data", {}) or {}
            if on_event is not None:
                try:
                    rv = on_event(goal, kind, data)
                    if inspect.isawaitable(rv):
                        await rv
                except Exception:
                    # 进度旁路失败不应影响子员工主流程。
                    pass
            if kind == "tool_call":
                steps.append(str(data.get("tool", "?")))
            elif kind == "tool_result" and data.get("ok") is False:
                failed.append(str(data.get("tool", "?")))
            elif kind == "model_message" and data.get("text"):
                final_text = str(data["text"])  # 兜底，done 的 final_text 会覆盖
            elif kind == "budget_exceeded":
                # 预算硬墙：先于 DONE 到达，标记终态并记下用量快照。
                status = STATUS_BUDGET_EXCEEDED
                if isinstance(data.get("budget"), dict):
                    budget_snap = data["budget"]
            elif kind == "done":
                if data.get("final_text"):
                    final_text = str(data["final_text"])
                if data.get("reason") == "budget_exceeded":
                    status = STATUS_BUDGET_EXCEEDED
                if isinstance(data.get("budget"), dict):
                    budget_snap = data["budget"]
            elif kind == "error":
                status = "error"

    try:
        if timeout is None:
            await _drive()
        else:
            await asyncio.wait_for(_drive(), timeout=timeout)
    except TimeoutError:
        status = "timeout"

    final_text = final_text.strip()
    ok = bool(final_text) and status == "done"
    return WorkerResult(
        goal=goal,
        ok=ok,
        final_text=final_text,
        granted_tools=granted_names,
        denied=unknown + failed,
        steps=steps,
        status=status,
        budget=budget_snap,
    )
