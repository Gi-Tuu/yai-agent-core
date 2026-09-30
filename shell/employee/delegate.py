"""delegate 工具：根员工把若干明确子目标并行委派给子员工。

复用内核既有工具机制——一个带 async handler 的普通 :class:`ToolSpec`，不改 Agent Loop：
模型调用 ``delegate`` → handler 在薄壳内并行起子 run → 汇总结果作为 tool observation 回灌，
根员工据此对用户负责。
"""

from __future__ import annotations

import asyncio

from shell.employee.worker import (
    DEFAULT_WORKER_ITERS,
    MAX_CONCURRENT,
    run_worker,
)
from yai_core.types import ToolSpec

#: 委派工具名（注册进根 registry，模型在 function-calling 中可见）。
DELEGATE_TOOL = "delegate"


def build_delegate_tool(root_core, *, depth: int = 1) -> ToolSpec:
    """构造 delegate 工具：async handler 闭包绑定根 core 与当前深度。"""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def _handler(tasks):  # noqa: ANN001 - 入参形状以 input_schema 为准
        normalized = _normalize_tasks(tasks)

        async def _run_one(item: dict):
            async with semaphore:
                return await run_worker(
                    root_core,
                    goal=item["goal"],
                    tools=item.get("tools"),
                    max_iters=item.get("max_iters") or DEFAULT_WORKER_ITERS,
                    depth=depth,
                    register_delegate=True,
                )

        results = await asyncio.gather(
            *[_run_one(item) for item in normalized]
        )
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
            "直接联系用户、不能自行发现工具；一次最多并行 5 个、最多嵌套两层。有先后"
            "依赖、或需要与用户确认的步骤不要委派，由你自己处理。"
        ),
        input_schema=schema,
        handler=_handler,
        source="native",
    )


def _normalize_tasks(tasks) -> list[dict]:
    """容错：把模型传入的 tasks 规整为 [{goal, tools?, max_iters?}]，丢弃无 goal 项。"""
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
                "tools": list(tools) if tools else None,
                "max_iters": item.get("max_iters"),
            }
        )
    return normalized


def attach_delegate(root_core, *, depth: int = 1) -> None:
    """给根/子 core 注册 delegate 工具（已注册则跳过，避免重复）。"""
    if root_core.registry.has(DELEGATE_TOOL):
        return
    root_core.registry.register(build_delegate_tool(root_core, depth=depth))
