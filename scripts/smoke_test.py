"""端到端冒烟：同一 Core 嵌入两个不同宿主，零 Agent 代码各自长出能力。

运行：python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from demo_model import OfflineScriptedModel  # noqa: E402
from host_a_notes import capabilities as notes_app  # noqa: E402
from host_c_companion import capabilities as companion_app  # noqa: E402
from yai_core import AgentCore, EventType  # noqa: E402

_VISIBLE = (
    EventType.STRATEGY_SELECTED,
    EventType.PLAN_CREATED,
    EventType.TOOL_CALL,
    EventType.MODEL_MESSAGE,
    EventType.DONE,
)


async def run_host(title: str, host: object, task: str) -> None:
    print(f"\n===== {title} =====")
    core = AgentCore.auto(host, OfflineScriptedModel())
    try:
        # 默认装配仅注册宿主业务工具；不能把模型文字或内部事件当成工具执行成功。
        tools = {t["name"] for t in core.list_tools()}
        print(f"自动发现工具: {sorted(tools)}")
        events = []
        async for event in core.astream(task):
            events.append(event)
            if event.type in _VISIBLE:
                print(f"  {event.type.value}: {event.data}")

        for event in events:
            if (
                event.type in (EventType.ERROR, EventType.BUDGET_EXCEEDED)
                or (event.type == EventType.TOOL_RESULT and event.data.get("ok") is not True)
                or (
                    event.type == EventType.DONE
                    and event.data.get("reason") == "budget_exceeded"
                )
            ):
                raise RuntimeError(f"{title} 冒烟失败: {event.type.value}: {event.data}")
        if not any(event.type == EventType.DONE for event in events):
            raise RuntimeError(f"{title} 冒烟失败: 缺少 DONE")
        if not any(
            event.type == EventType.TOOL_RESULT
            and event.data.get("ok") is True
            and event.data.get("tool") in tools
            for event in events
        ):
            raise RuntimeError(f"{title} 冒烟失败: 没有成功的宿主业务工具调用")
    finally:
        await core.aclose()


async def main() -> None:
    await run_host("宿主 A 笔记应用", notes_app, "搜索笔记里和项目有关的内容")
    await run_host(
        "宿主 C AI 陪伴（AMBRACE 预演）",
        companion_app,
        "先查看小拥的状态，然后检索和约定有关的记忆，给出主动关怀建议",
    )
    print("\n冒烟测试通过：同一内核已自适应两个不同宿主。")


if __name__ == "__main__":
    asyncio.run(main())
