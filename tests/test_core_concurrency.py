"""并发模型契约测试（离线，零网络）。

契约：一个 ``AgentCore`` 实例 = 一条串行会话，实例内任务经 ``_task_lock`` 互斥；
多条相互独立、需要并发的会话应实例化各自的 ``AgentCore``（可共享无状态模型）。
"""

import asyncio
import time

from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec


class ScriptedModel:
    """按预设顺序返回响应的假模型。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """两个 [start, end] 区间是否实质交叠（留 1ms 容差）。"""
    return a[0] < b[1] - 0.001 and b[0] < a[1] - 0.001


def _make_slow_tool(intervals: list[tuple[float, float]], delay: float = 0.2):
    async def slow_job() -> dict:
        """一个会主动让出事件循环的异步工具。"""
        start = time.monotonic()
        await asyncio.sleep(delay)
        end = time.monotonic()
        intervals.append((start, end))
        return {"ok": True}

    return slow_job


def _tool_call() -> ToolCallRequest:
    return ToolCallRequest(id="c1", name="slow_job", arguments={})


def test_same_core_serializes_concurrent_runs() -> None:
    """同一实例并发两个任务：工具执行区间不得交叠，总耗时≈两者之和（排队）。"""
    intervals: list[tuple[float, float]] = []
    core = AgentCore(
        ScriptedModel(
            [
                ModelResponse(content="", tool_calls=[_tool_call()]),
                ModelResponse(content="甲完成"),
                ModelResponse(content="", tool_calls=[_tool_call()]),
                ModelResponse(content="乙完成"),
            ]
        )
    )
    core.register_tools([build_spec(_make_slow_tool(intervals))])

    async def main() -> float:
        start = time.monotonic()
        await asyncio.gather(core.run("查询任务甲"), core.run("查询任务乙"))
        return time.monotonic() - start

    elapsed = asyncio.run(main())

    assert len(intervals) == 2
    assert not _overlap(intervals[0], intervals[1]), "同一 core 的两个任务发生了交错"
    assert elapsed >= 0.36, f"串行应至少 0.4s，实际 {elapsed:.3f}s"


def test_separate_core_instances_run_in_parallel() -> None:
    """两个独立实例并发：工具区间应交叠、总耗时≈单次（并发=多实例的契约）。"""
    intervals: list[tuple[float, float]] = []
    spec = build_spec(_make_slow_tool(intervals))

    def make_core() -> AgentCore:
        model = ScriptedModel(
            [ModelResponse(content="", tool_calls=[_tool_call()]), ModelResponse(content="完成")]
        )
        core = AgentCore(model)
        core.register_tools([spec])
        return core

    core_a, core_b = make_core(), make_core()

    async def main() -> float:
        start = time.monotonic()
        await asyncio.gather(core_a.run("查询任务甲"), core_b.run("查询任务乙"))
        return time.monotonic() - start

    elapsed = asyncio.run(main())

    assert len(intervals) == 2
    assert _overlap(intervals[0], intervals[1]), "独立 core 实例应当能并行"
    assert elapsed < 0.32, f"并行应约 0.2s，实际 {elapsed:.3f}s"


def test_closing_astream_early_releases_lock() -> None:
    """流式宿主提前关闭事件流后，锁必须释放，后续任务不得被永久阻塞。"""
    intervals: list[tuple[float, float]] = []

    class ReactOnceModel:
        """看到 tool 结果就收尾，否则发起一次工具调用（可重入、不依赖调用次序）。"""

        async def achat(self, messages, tools=None, *, tier="standard"):
            if any(isinstance(m, dict) and m.get("role") == "tool" for m in (messages or [])):
                return ModelResponse(content="完成")
            return ModelResponse(content="", tool_calls=[_tool_call()])

    core = AgentCore(ReactOnceModel())
    core.register_tools([build_spec(_make_slow_tool(intervals, delay=0.05))])

    async def main():
        agen = core.astream("查询第一件")
        await agen.__anext__()  # 取走首个事件（strategy_selected）
        await agen.aclose()     # 提前关闭：应在 __aexit__ 释放实例锁
        # 若锁未释放，这里会一直等到 wait_for 超时。
        return await asyncio.wait_for(core.run("查询第二件"), timeout=3.0)

    result = asyncio.run(main())
    assert result.final_text
