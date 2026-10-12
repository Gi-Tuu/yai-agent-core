"""P0-2：模型调用硬超时与取消传播，全部离线、零网络、零 API Key。

覆盖三件事：
1. 模型挂起超过 ``model_timeout`` → wait_for 先取消挂起调用，loop 发 ERROR 事件
   体面收尾（不裸抛穿透宿主），且实例级任务锁随之释放、同一 core 可继续跑下一单；
2. ``model_timeout=None`` 关闭硬超时，慢但成功的调用照常返回；
3. 外层任务取消（用户终止 / aclose）→ CancelledError 干净传播、真正中断挂起的
   模型调用，而不是被改写成超时或被 ``except Exception`` 吞掉。
"""

import asyncio
import time

import pytest

from yai_core import AgentCore, ModelResponse


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
