"""P0-2：模型调用硬超时与取消传播，全部离线、零网络、零 API Key。

覆盖四件事：
1. 模型挂起超过 ``model_timeout`` → 超时先取消挂起调用，loop 发 ERROR 事件
   体面收尾（不裸抛穿透宿主），且实例级任务锁随之释放、同一 core 可继续跑下一单；
2. ``model_timeout=None`` 关闭硬超时，慢但成功的调用照常返回；
3. 外层任务取消（用户终止 / aclose）→ CancelledError 干净传播、真正中断挂起的
   模型调用，而不是被改写成超时或被 ``except Exception`` 吞掉；
4. 取消恰好落在"模型调用已完成、消费任务尚未恢复"的窗口时也不能被吞掉——
   3.11 的 ``asyncio.wait_for`` 在该分支会 ``return fut.result()``，被吃掉的
   取消意味着取消本该中断的工具会一路跑到底。
"""

import asyncio
import time

import pytest

from yai_core import AgentCore, ModelResponse, build_spec
from yai_core.types import ToolCallRequest


class SlowThenOk:
    """第一次调用长时间挂起（并记录自己是否被取消），之后恢复正常。"""

    def __init__(self, delay: float = 1.0) -> None:
        self.delay = delay
        self.calls = 0
        self.cancelled = False

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                # wait_for 超时或外层取消都应当把取消送达挂起的模型调用。
                self.cancelled = True
                raise
            return ModelResponse(content="不应到达的慢响应")
        return ModelResponse(content="恢复正常")


class SlowOk:
    """固定延迟后成功，用于验证关闭超时时不被误杀。"""

    def __init__(self, delay: float = 0.15) -> None:
        self.delay = delay

    async def achat(self, messages, tools=None, *, tier="standard"):
        await asyncio.sleep(self.delay)
        return ModelResponse(content="慢但成功")


class CancelOnModelDone:
    """模型调用刚完成时取消消费任务，复现"完成瞬间"的取消竞态窗口。

    取消通过 ``call_soon`` 发出：它一定落在"模型调用已完成、消费任务还没恢复"
    的那个窗口里，因此不依赖调度运气，每次都能命中同一条竞态路径。
    """

    yai_multimodal = True

    def __init__(self) -> None:
        self.calls = 0
        self.target: asyncio.Task | None = None

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            asyncio.get_running_loop().call_soon(self.target.cancel)
            return ModelResponse(
                tool_calls=[ToolCallRequest(id="c1", name="slow_tool", arguments={})]
            )
        return ModelResponse(content="收尾")


def test_model_timeout_ends_gracefully_and_frees_lock() -> None:
    async def scenario() -> tuple[SlowThenOk, object, object, float]:
        model = SlowThenOk(delay=1.0)
        core = AgentCore(model, model_timeout=0.1)
        start = time.monotonic()
        first = await core.run("你好")
        elapsed = time.monotonic() - start
        # 超时后锁必须已释放：同一 core 立刻能跑下一单，第二次调用恢复正常。
        second = await core.run("再来一次")
        return model, first, second, elapsed

    model, first, second, elapsed = asyncio.run(scenario())

    # 没有干等满 1 秒：约在超时点（0.1s）即终止。
    assert elapsed < 0.6
    # 挂起的模型调用确实被 wait_for 取消，没有残留后台协程。
    assert model.cancelled is True
    first_types = [e.type.value for e in first.events]
    assert "error" in first_types
    error = next(e for e in first.events if e.type.value == "error")
    assert "超时" in error.data.get("error", "")

    second_types = [e.type.value for e in second.events]
    assert "error" not in second_types
    done = next(e for e in second.events if e.type.value == "done")
    assert done.data.get("final_text") == "恢复正常"


def test_none_timeout_disables_deadline() -> None:
    async def scenario() -> tuple[object, float]:
        model = SlowOk(delay=0.15)
        core = AgentCore(model, model_timeout=None)
        start = time.monotonic()
        result = await core.run("你好")
        return result, time.monotonic() - start

    result, elapsed = asyncio.run(scenario())

    # 确实等够了模型延迟，没有被任何超时打断。
    assert elapsed >= 0.14
    types = [e.type.value for e in result.events]
    assert "error" not in types
    done = next(e for e in result.events if e.type.value == "done")
    assert done.data.get("final_text") == "慢但成功"


def test_outer_cancellation_propagates_to_model() -> None:
    async def scenario() -> bool:
        # 超时上限给得足够大，确保中断来自外层 cancel 而非 wait_for 超时。
        model = SlowThenOk(delay=10.0)
        core = AgentCore(model, model_timeout=60.0)
        agent = core.astream("你好")

        async def drain() -> None:
            async for _ in agent:
                pass

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.2)  # 确保已进入第一次模型调用的挂起 await。
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await agent.aclose()  # 幂等：确认生成器关闭、实例锁释放。

        # 取消后同一 core 仍可正常接单（锁未泄漏），第二次调用恢复正常。
        recovered = await core.run("恢复")
        recovered_types = [e.type.value for e in recovered.events]
        assert "error" not in recovered_types
        return model.cancelled

    assert asyncio.run(scenario()) is True


def test_cancel_at_model_completion_boundary_is_not_swallowed() -> None:
    """取消落在"模型调用刚完成"的窗口时，必须真正中断随后的工具调用。

    回归 3.11 的 ``asyncio.wait_for`` 竞态：内层恰好完成时它会走
    ``if fut.done(): return fut.result()``，把外层取消吃掉。被吃掉的取消会让
    本该中断的工具一路跑到底——委派里"一个子任务失败要中断另一个子任务"
    正是靠取消生效，吞掉就等于没人中断，整个委派会挂住。
    """

    async def scenario() -> int:
        release = asyncio.Event()
        interrupted = asyncio.Event()
        calls = {"tool": 0}

        async def slow_tool() -> str:
            calls["tool"] += 1
            try:
                await release.wait()
                return "跑完了"
            finally:
                interrupted.set()

        model = CancelOnModelDone()
        core = AgentCore(model, llm_router=False)
        core.register_tools([build_spec(slow_tool)])

        async def drain() -> None:
            async for _ in core.astream("跑一下慢工具"):
                pass

        task = asyncio.create_task(drain())
        model.target = task
        try:
            await asyncio.wait_for(interrupted.wait(), timeout=5)
        except TimeoutError:
            release.set()
            await asyncio.sleep(0)
            pytest.fail("外部取消被吞掉：在飞的工具没有中断")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return calls["tool"]

    # 取消打断了工具，且工具只被调用了一次（没有被取消后续跑第二遍）。
    assert asyncio.run(scenario()) == 1
