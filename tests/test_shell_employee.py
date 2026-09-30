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
