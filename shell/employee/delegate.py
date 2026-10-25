"""delegate 工具：根员工把若干明确子目标并行委派给子员工。

复用内核既有工具机制——一个带 async handler 的普通 :class:`ToolSpec`，不改 Agent Loop：
模型调用 ``delegate`` → handler 在薄壳内并行起子 run → 汇总结果作为 tool observation 回灌，
根员工据此对用户负责。
"""

from __future__ import annotations

import asyncio

from shell.employee.worker import (
    DEFAULT_WORKER_ITERS,
    DEFAULT_WORKER_TIMEOUT,
    MAX_CONCURRENT,
    default_budget,
    run_worker,
)
from yai_core.kernel.budget import Budget
from yai_core.types import ToolSpec

#: 委派工具名（注册进根 registry，模型在 function-calling 中可见）。
DELEGATE_TOOL = "delegate"


class DelegationSupervisor:
    """跨层级共享的委派并发闸。

    根员工首次 :func:`attach_delegate` 时创建一个，随后沿 delegate 链原样透传给
    子、孙员工——所有层级的子员工在派生时都争抢**同一把**信号量，从而把"根并行
    5 个、每个子又并行 5 个"的理论 5+25 超额收敛为全局同时存活 ≤ ``max_concurrent``。
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT) -> None:
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)


def build_delegate_tool(
    root_core, *, depth: int = 1, on_progress=None, supervisor: DelegationSupervisor | None = None,
    _on_failure=None,
) -> ToolSpec:
    """构造 delegate 工具：async handler 闭包绑定根 core、当前深度与可选进度回调。

    ``on_progress(goal, type, data)`` 为可选旁路（可为 async）：子员工每产生一个事件
    即回调，供灵动岛等上层实时显示"几个子员工在跑、跑到哪"；不传则无实时进度。

    ``supervisor`` 为跨层级共享并发闸；为 None 时新建一把（顶层根首次挂载的常态），
    派生下级员工时务必把同一个 supervisor 透传下去，否则并发闸会退化为"每层一把"。
    """
    if supervisor is None:
        supervisor = DelegationSupervisor()
    semaphore = supervisor.semaphore

    async def _handler(tasks):  # noqa: ANN001 - 入参形状以 input_schema 为准
        subject = root_core._task_subject
        normalized = _normalize_tasks(tasks)

        async def _run_one(item: dict):
            iters = item.get("max_iters") or DEFAULT_WORKER_ITERS
            budget = _budget_for_item(item, iters)
            async with semaphore:
                return await run_worker(
                    root_core,
                    goal=item["goal"],
                    tools=item.get("tools"),
                    max_iters=iters,
                    depth=depth,
                    register_delegate=True,
                    timeout=item.get("timeout") or DEFAULT_WORKER_TIMEOUT,
                    on_event=on_progress,
                    budget=budget,
                    supervisor=supervisor,
                    subject=subject,
                )

        pending = [asyncio.create_task(_run_one(item)) for item in normalized]
        try:
            results = await asyncio.gather(*pending)
        except BaseException:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise
        if _on_failure is not None and any(not result.ok for result in results):
            _on_failure()
        return {"results": [r.to_observation() for r in results]}

    schema = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": "要并行委派的子任务列表，每项是一个明确、可独立完成的子目标。",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "该子员工要完成的单一目标（一句话，明确可验证）。",
                        },
                        "tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "最小授权的工具名白名单（必须是根已注册工具）；"
                                "省略则只授予只读类工具。"
                            ),
                        },
                        "max_iters": {
                            "type": "integer",
                            "description": "该子员工的最大迭代次数（默认 3，越小越省）。",
                        },
                        "max_model_calls": {
                            "type": "integer",
                            "description": (
                                "该子员工的模型调用硬上限（省略则按 max_iters 推导，"
                                "默认 max_iters+2）；触顶即熔断并回流 budget_exceeded。"
                            ),
                        },
                        "max_tool_calls": {
                            "type": "integer",
                            "description": (
                                "该子员工的业务工具调用硬上限（省略则按 max_iters 推导，"
                                "默认 max_iters*4）；用于防止单个子员工失控狂调工具。"
                            ),
                        },
                        "timeout": {
                            "type": "number",
                            "description": (
                                "该子员工的墙钟超时秒数（默认 60）；超时即取消并在"
                                "结果里标 status=timeout，不影响其他并行子员工。"
                            ),
                        },
                    },
                    "required": ["goal"],
                },
            }
        },
        "required": ["tasks"],
    }
    return ToolSpec(
        name=DELEGATE_TOOL,
        description=(
            "把一个复杂任务拆成若干彼此独立、目标明确的子任务，并行委派给子员工完成，"
            "再由你（根员工）汇总结果并对用户负责。子员工只能使用你授予的工具、不能"
            "直接联系用户、不能自行发现工具；全局同时最多 5 个子员工在跑（含嵌套层级）、"
            "最多嵌套两层。每个子员工都有模型/工具调用硬预算，可用 max_model_calls/"
            "max_tool_calls 显式收紧，触顶以 status=budget_exceeded 回流已完成部分。"
            "有先后依赖、或需要与用户确认的步骤不要委派，由你自己处理。结果会给出每个"
            "子员工的 status（done/timeout/error/budget_exceeded）、steps（实际调用）、"
            "budget（用量快照）与 denied（缺失/失败工具）：若因缺工具或预算未完成，"
            "你可补齐工具/放宽预算后再次委派，或就缺失能力向用户申请。"
        ),
        input_schema=schema,
        handler=_handler,
        source="native",
    )


def _as_positive_int(value) -> int | None:
    """把模型传入的可选整数上限容错成正整数；非法 / 缺省返回 None（走默认推导）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _budget_for_item(item: dict, max_iters: int) -> Budget:
    """按委派任务的可选覆盖项构造子员工预算；未覆盖维度用默认推导补齐。"""
    base = default_budget(max_iters)
    model_cap = _as_positive_int(item.get("max_model_calls"))
    tool_cap = _as_positive_int(item.get("max_tool_calls"))
    if model_cap is None and tool_cap is None:
        return base
    return Budget(
        max_model_calls=base.max_model_calls if model_cap is None else model_cap,
        max_tool_calls=base.max_tool_calls if tool_cap is None else tool_cap,
        max_tokens=None,
    )


def _normalize_tasks(tasks) -> list[dict]:
    """容错：把模型传入的 tasks 规整为 [{goal, tools?, max_iters?, ...}]，丢弃无 goal 项。"""
    if isinstance(tasks, dict):
        tasks = [tasks]
    normalized: list[dict] = []
    for item in tasks or []:
        if isinstance(item, str):
            item = {"goal": item}
        if not isinstance(item, dict):
            continue
        goal = str(item.get("goal", "")).strip()
        if not goal:
            continue
        tools = item.get("tools")
        if isinstance(tools, str):
            tools = [tools]
        normalized.append(
            {
                "goal": goal,
                "tools": list(tools) if tools is not None else None,
                "max_iters": item.get("max_iters"),
                "max_model_calls": _as_positive_int(item.get("max_model_calls")),
                "max_tool_calls": _as_positive_int(item.get("max_tool_calls")),
                "timeout": item.get("timeout"),
            }
        )
    return normalized


def attach_delegate(
    root_core,
    *,
    depth: int = 1,
    on_progress=None,
    supervisor: DelegationSupervisor | None = None,
    _on_failure=None,
) -> DelegationSupervisor:
    """给根/子 core 注册 delegate 工具（已注册则跳过，避免重复）。

    返回本次挂载所用的 :class:`DelegationSupervisor`：顶层根挂载时通常不传
    supervisor（函数新建一把并返回），派生孙员工时把同一个 supervisor 透传进来，
    保证全层级共用一个并发闸。
    """
    if root_core.registry.has(DELEGATE_TOOL):
        return supervisor if supervisor is not None else DelegationSupervisor()
    if supervisor is None:
        supervisor = DelegationSupervisor()
    root_core.registry.register(
        build_delegate_tool(
            root_core, depth=depth, on_progress=on_progress, supervisor=supervisor,
            _on_failure=_on_failure,
        )
    )
    return supervisor
