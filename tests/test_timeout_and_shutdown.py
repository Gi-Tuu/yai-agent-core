"""工具 handler 超时与 AgentCore 收尾（aclose）的回归。

两条都是"运维时才想起来、平时看不见"的能力，所以断言刻意钉在**行为差别**上：
默认关闭时一字不差地保持旧行为；打开时超时要能被模型看懂；
收尾默认绝不碰宿主共享的对象。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from yai_core import AgentCore, CoreConfig, ModelResponse, ScriptedModel, build_spec
from yai_core.config import CoreConfig as Config
from yai_core.policy import AllowlistPolicy
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.tools.executor import ToolTimeoutError
from yai_core.types import EventType


class _Channel:
    def __init__(self) -> None:
        self.closed = 0

    async def emit(self, event) -> None:  # pragma: no cover
        return None

    async def ask(self, question: str) -> str:  # pragma: no cover
        return ""

    async def confirm(self, tool_name: str, arguments: dict) -> bool:  # pragma: no cover
        return True

    def close(self) -> None:
        self.closed += 1


def _executor(handler, *, timeout: float | None = None) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(build_spec(handler))
    return ToolExecutor(
        registry, AllowlistPolicy((), mode="auto"), _Channel(), handler_timeout=timeout
    )


# ---------- 异步 handler 超时：真的被取消 ----------


def test_async_handler_timeout_cancels_and_reports() -> None:
    cancelled = {"flag": False}

    async def slow_job(seconds: float) -> dict:
        """慢任务。"""
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            cancelled["flag"] = True
            raise
        return {"done": True}

    events, ok, text = asyncio.run(
        _executor(slow_job, timeout=0.05).execute("slow_job", {"seconds": 5})
    )
    assert ok is False
    assert "超过 0.05s" in text and "已取消" in text
    assert "超过 0.05s" in text and "已取消" in text
    assert cancelled["flag"] is True                 # 协程确实收到了 CancelledError
    result = next(e for e in events if e.type == EventType.TOOL_RESULT)
    assert result.data["ok"] is False and result.data["timed_out"] is True


def test_async_handler_within_timeout_is_unaffected() -> None:
    async def quick() -> dict:
        """快任务。"""
        await asyncio.sleep(0.01)
        return {"ok": True}

    events, ok, text = asyncio.run(_executor(quick, timeout=2.0).execute("quick", {}))
    assert ok is True and '"ok": true' in text


# ---------- 同步 handler 超时：止损而非中断 ----------


def test_sync_handler_timeout_does_not_block_forever() -> None:
    finished = {"flag": False}

    def blocking_job(seconds: float) -> dict:
        """阻塞任务。"""
        time.sleep(seconds)
        finished["flag"] = True
        return {"ok": True}

    executor = _executor(blocking_job, timeout=0.05)

    async def main() -> None:
        started = time.monotonic()
        _events, ok, text = await executor.execute("blocking_job", {"seconds": 0.4})
        elapsed = time.monotonic() - started
        # 关键断言在"回话时刻"：execute 立刻返回，而不是等线程跑完 0.4s。
        assert ok is False
        assert elapsed < 0.3, f"execute 应立刻回话，实际等了 {elapsed:.2f}s"
        assert "同步工具无法中断" in text and "仍在后台" in text
        assert finished["flag"] is False     # 此刻后台线程仍在跑，副作用尚未落地
        await asyncio.sleep(0.45)          # 线程总共要睡 0.4s
        assert finished["flag"] is True      # 但它确实还在跑，之后照样落地

    asyncio.run(main())


def test_no_timeout_keeps_sync_handler_on_loop_thread() -> None:
    """默认关闭超时 = 旧行为：同步 handler 就在事件循环线程里直接跑，不进线程池。"""
    seen: dict[str, int] = {}

    def whoami() -> dict:
        """报告执行线程。"""
        seen["tid"] = threading.get_ident()
        return {"ok": True}

    _events, ok, _text = asyncio.run(_executor(whoami).execute("whoami", {}))
    assert ok is True
    assert seen["tid"] == threading.get_ident()


def test_timeout_rejects_bad_values() -> None:
    def noop() -> dict:
        """空工具。"""
        return {"ok": True}

    for bad in (0, -1, True, "3"):
        with pytest.raises(ValueError, match="handler_timeout"):
            _executor(noop, timeout=bad)


def test_timeout_error_is_a_runtime_error_subclass() -> None:
    assert issubclass(ToolTimeoutError, RuntimeError)


# ---------- 组合工具的每一步都继承超时 ----------


def test_composite_step_inherits_handler_timeout() -> None:
    from yai_core.tools.composer import build_composite_spec

    async def slow() -> dict:
        """慢步骤。"""
        await asyncio.sleep(3)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(build_spec(slow))
    registry.register(
        build_composite_spec("wrap", "包一层", [{"tool": "slow", "args": {}}])
    )
    executor = ToolExecutor(
        registry, AllowlistPolicy((), mode="auto"), _Channel(), handler_timeout=0.05
    )
    _events, ok, text = asyncio.run(executor.execute("wrap", {}))
    assert ok is False and "超过 0.05s" in text


# ---------- CoreConfig / AgentCore 接线 ----------


def test_core_config_validates_tool_timeout() -> None:
    assert Config(tool_timeout=3.0).validate().tool_timeout == 3.0
    assert Config(tool_timeout=None).validate().tool_timeout is None
    for bad in (0, -1, True, "3"):
        with pytest.raises(ValueError, match="tool_timeout"):
            Config(tool_timeout=bad).validate()


def test_agent_core_passes_tool_timeout_to_executor() -> None:
    core = AgentCore(ScriptedModel([ModelResponse(content="ok")]), tool_timeout=1.25)
    assert core.executor.handler_timeout == 1.25
    assert AgentCore(ScriptedModel([ModelResponse(content="ok")])).executor.handler_timeout is None


def test_core_config_from_config_wires_tool_timeout() -> None:
    config = CoreConfig(tool_timeout=2.5)
    core = AgentCore.from_config(ScriptedModel([ModelResponse(content="ok")]), config)
    assert core.executor.handler_timeout == 2.5


# ---------- aclose ----------


class _ACloseable:
    """只提供 async aclose() 的资源（新式）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def aclose(self) -> None:
        self.calls += 1
        if self.fail:
            raise OSError("关闭失败")


class _CCloseable:
    """只提供同步 close() 的资源（旧式，如 SQLiteStore.close）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def close(self) -> None:
        self.calls += 1
        if self.fail:
            raise OSError("关闭失败")


class _Model(_ACloseable):
    async def achat(self, messages, tools=None, *, tier="standard"):
        return ModelResponse(content="ok")


class _BlockingModel(_Model):
    """第一次 achat 卡在事件上，用来验证 aclose 会等当前任务。"""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def achat(self, messages, tools=None, *, tier="standard"):
        await self.gate.wait()
        return ModelResponse(content="ok")


def _core_with(**shared: Any) -> AgentCore:
    model = shared.pop("model", None) or _Model()
    return AgentCore(model, **shared)


def test_aclose_leaves_host_supplied_objects_alone_by_default() -> None:
    model, channel, memory = _Model(), _ACloseable(), _ACloseable()
    core = _core_with(model=model, channel=channel, memory=memory)
    asyncio.run(core.aclose())
    assert (model.calls, channel.calls, memory.calls) == (0, 0, 0)


def test_aclose_close_shared_walks_the_graph_once() -> None:
    model, channel, memory = _Model(), _ACloseable(), _ACloseable()
    discovery, sandbox = _ACloseable(), _CCloseable()
    core = _core_with(
        model=model, channel=channel, memory=memory, discovery=discovery, sandbox=sandbox
    )
    asyncio.run(core.aclose(close_shared=True))
    assert (channel.calls, discovery.calls, sandbox.calls, memory.calls, model.calls) == (
        1,
        1,
        1,
        1,
        1,
    )
    asyncio.run(core.aclose(close_shared=True))   # 幂等：第二次不再动
    assert channel.calls == 1


def test_aclose_reports_every_failure_but_attempts_all() -> None:
    model = _Model()
    bad_channel, bad_memory = _ACloseable(fail=True), _ACloseable(fail=True)
    core = _core_with(model=model, channel=bad_channel, memory=bad_memory)
    with pytest.raises(RuntimeError, match="收尾时以下资源关闭失败") as exc:
        asyncio.run(core.aclose(close_shared=True))
    message = str(exc.value)
    assert message.count("关闭失败") >= 2
    assert model.calls == 1                       # 前面报错也没跳过后面的资源


def test_aclose_waits_for_the_running_task() -> None:
    """aclose 拿串行锁：任务在跑时不会半途关资源。"""
    channel = _Channel()          # 必须是真 Channel 替身，否则任务一启动就报错退出
    model = _BlockingModel()
    core = _core_with(model=model, channel=channel)

    async def main() -> None:
        async def run_task() -> None:
            async for _ in core.astream("做点事"):
                pass

        task = asyncio.create_task(run_task())
        await asyncio.sleep(0.02)                 # 让任务进锁并卡在模型上
        closer = asyncio.create_task(core.aclose(close_shared=True))
        await asyncio.sleep(0.02)
        assert not closer.done(), "aclose 不该在当前任务结束前抢先关闭"
        assert channel.closed == 0
        model.gate.set()
        await task
        await closer

    asyncio.run(main())
    assert channel.closed == 1


def test_default_built_resources_are_owned_and_closed() -> None:
    core = _core_with()                            # 未传 channel/memory → core 自建
    assert len(core._owned) >= 1                   # 自建的默认实现归本实例负责
    asyncio.run(core.aclose())
    assert core._closed is True
