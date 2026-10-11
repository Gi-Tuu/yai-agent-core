"""S2 子员工委派的离线测试：不依赖网络 / API Key。

覆盖：子员工运行回流、工具白名单收窄、delegate 并行委派、深度上限、
默认只读工具启发式、未知工具上报。
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from shell.employee import (  # noqa: E402
    MAX_DEPTH,
    attach_delegate,
    build_worker_core,
    default_read_tools,
    run_worker,
)

from yai_core import AgentCore, ModelResponse, build_spec  # noqa: E402
from yai_core.llm.scripted import ScriptedModel  # noqa: E402
from yai_core.types import ToolCallRequest  # noqa: E402


def list_items() -> list[str]:
    """列出全部条目。"""
    return ["a", "b"]


def get_item(key: str) -> dict:
    """读取单个条目。"""
    return {"key": key, "value": key.upper()}


def delete_item(key: str) -> dict:
    """删除一个条目。"""
    return {"deleted": key}


def _root(responses: list[ModelResponse]) -> AgentCore:
    core = AgentCore(ScriptedModel(responses))
    core.register_tools(
        [build_spec(list_items), build_spec(get_item), build_spec(delete_item)]
    )
    return core


def test_worker_runs_and_returns() -> None:
    root = _root([ModelResponse(content="共 2 条")])

    result = asyncio.run(
        run_worker(root, goal="列出条目数量", tools=["list_items"])
    )

    assert result.ok
    assert result.final_text == "共 2 条"
    assert result.granted_tools == ["list_items"]


def test_worker_tools_are_narrowed() -> None:
    root = _root([ModelResponse(content="ok")])

    worker = build_worker_core(root, goal="列出条目", tools=["list_items"])

    assert worker.registry.has("list_items")
    assert not worker.registry.has("get_item")
    assert not worker.registry.has("delete_item")
    assert worker.policy.mode == "deny_all"


def test_delegate_parallel_returns_observations() -> None:
    root = _root(
        [
            ModelResponse(content="子结果一"),
            ModelResponse(content="子结果二"),
        ]
    )
    attach_delegate(root, depth=1)
    delegate = root.registry.get("delegate")

    out = asyncio.run(
        delegate.handler(
            tasks=[
                {"goal": "子目标一", "tools": ["list_items"]},
                {"goal": "子目标二", "tools": ["get_item"]},
            ]
        )
    )

    results = out["results"]
    assert [r["goal"] for r in results] == ["子目标一", "子目标二"]
    assert all(r["ok"] for r in results)
    assert results[0]["result"] == "子结果一"
    assert results[1]["granted_tools"] == ["get_item"]


def test_depth_limit_removes_delegate_at_max() -> None:
    root = _root([ModelResponse(content="ok")])

    w1 = build_worker_core(
        root, goal="x", tools=["list_items"], depth=1, register_delegate=True
    )
    w2 = build_worker_core(
        root,
        goal="x",
        tools=["list_items"],
        depth=MAX_DEPTH,
        register_delegate=True,
    )

    assert w1.registry.has("delegate")
    assert not w2.registry.has("delegate")


def test_default_read_tools_prefix() -> None:
    names = ["list_items", "get_item", "delete_item"]
    assert default_read_tools(names) == ["list_items", "get_item"]


def test_unknown_tool_reported_denied() -> None:
    root = _root([ModelResponse(content="我直接回答")])

    result = asyncio.run(
        run_worker(root, goal="汇报当前情况", tools=["ghost_tool"])
    )

    assert "ghost_tool" in result.denied


def test_worker_steps_recorded() -> None:
    root = _root(
        [
            ModelResponse(
                tool_calls=[
                    ToolCallRequest(id="c1", name="list_items", arguments={})
                ]
            ),
            ModelResponse(content="共 2 条"),
        ]
    )

    result = asyncio.run(run_worker(root, goal="列出条目", tools=["list_items"]))
    obs = result.to_observation()

    assert result.ok
    assert result.status == "done"
    assert result.steps == ["list_items"]
    assert obs["steps"] == ["list_items"]
    assert obs["status"] == "done"


def test_worker_timeout_marks_status() -> None:
    async def slow_tool() -> str:
        """故意长时间运行，用于触发墙钟超时。"""
        await asyncio.sleep(5)
        return "late"

    root = AgentCore(
        ScriptedModel(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCallRequest(id="c1", name="slow_tool", arguments={})
                    ]
                )
            ]
        )
    )
    root.register_tools([build_spec(slow_tool)])

    result = asyncio.run(
        run_worker(root, goal="调用慢工具并返回结果", tools=["slow_tool"], timeout=0.3)
    )

    assert result.status == "timeout"
    assert not result.ok
    # 内核在工具返回后才经执行器批量发出 tool_call；slow_tool 被超时取消、调用事件
    # 未及 yield，因此 steps 不记录这次未完成的调用（诚实反映事件时序）。
    assert result.steps == []


def test_on_event_callback_receives_progress() -> None:
    root = _root(
        [
            ModelResponse(
                tool_calls=[
                    ToolCallRequest(id="c1", name="list_items", arguments={})
                ]
            ),
            ModelResponse(content="done"),
        ]
    )
    seen: list[str] = []

    asyncio.run(
        run_worker(
            root,
            goal="列出条目",
            tools=["list_items"],
            on_event=lambda _goal, kind, _data: seen.append(kind),
        )
    )

    assert "tool_call" in seen
    assert "done" in seen


def test_delegate_timeout_does_not_block_gather() -> None:
    async def slow_tool() -> str:
        """故意长时间运行。"""
        await asyncio.sleep(5)
        return "late"

    class AlwaysSlow:
        """每次都要求调用 slow_tool（两个子员工共享，并行触发超时）。"""

        async def achat(self, messages, tools=None, *, tier: str = "standard"):
            return ModelResponse(
                tool_calls=[
                    ToolCallRequest(id="c", name="slow_tool", arguments={})
                ]
            )

    root = AgentCore(AlwaysSlow())
    root.register_tools([build_spec(slow_tool)])
    attach_delegate(root, depth=1)
    delegate = root.registry.get("delegate")

    loop = asyncio.new_event_loop()
    start = loop.time()
    out = loop.run_until_complete(
        delegate.handler(
            tasks=[
                {"goal": "慢一", "tools": ["slow_tool"], "timeout": 0.3},
                {"goal": "慢二", "tools": ["slow_tool"], "timeout": 0.3},
            ]
        )
    )
    elapsed = loop.time() - start
    loop.close()

    results = out["results"]
    assert [r["status"] for r in results] == ["timeout", "timeout"]
    assert all(not r["ok"] for r in results)
    # 关键：两个慢任务都被墙钟上限截断，gather 没有无限等待。
    assert elapsed < 3
