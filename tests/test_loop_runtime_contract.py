"""Batch 0 运行期契约回归：可信终态（DONE/status）、SPI 故障降级、限尾/召回边界。

覆盖审计修复：
- CORE-2：宿主 SPI（memory / channel / discovery / learning selector）失败仍产出
  终止事件；CancelledError 继续传播且不发 DONE；
- CORE-4：失败时 RunResult.status/error 与 DONE.status/error 可读；空 final_text
  不写 assistant 历史；
- F5：非模型异常不得把 ERROR.stage 标成 "model"；
- F6：限尾收尾仍返回结构化 tool_calls 时发 ERROR + 非空诚实文案；
- F9：任务起始主动召回套 model_timeout，超时降级不挂死；
- F11：单次 run 的收据链可独立校验（core 每 run 调用 executor.begin_run）。

全部离线：ScriptedModel / 假 SPIs，不访问网络、不需要 API Key。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from yai_core import (
    AgentCore,
    ModelResponse,
    ScriptedModel,
    build_spec,
    verify_receipt_chain,
)
from yai_core.channels import CollectChannel
from yai_core.kernel import AdaptiveRouter
from yai_core.kernel.router import RouteDecision
from yai_core.types import (
    ChatMessage,
    EventType,
    Strategy,
    ToolCallRequest,
)


def _run(coro):
    return asyncio.run(coro)


def _done(result):
    return [event for event in result.events if event.type == EventType.DONE][-1]


def _errors(result):
    return [event for event in result.events if event.type == EventType.ERROR]


def _roles(store) -> list[str]:
    return [message.role for message in store.history()]


def search_notes(keyword: str = "周报") -> dict:
    """按关键词搜索笔记。"""
    return {"hits": [keyword]}


def _tool_calls() -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=[ToolCallRequest(id="c1", name="search_notes", arguments={"keyword": "周报"})],
    )


# ---------- 正常收尾：status 加性字段 ----------


def test_success_run_carries_ok_status_and_no_error() -> None:
    core = AgentCore(ScriptedModel([ModelResponse(content="完成")]))

    result = _run(core.run("你好"))

    assert result.status == "ok"
    assert result.error is None
    done = _done(result)
    assert done.data["status"] == "ok"
    assert "error" not in done.data
    assert _roles(core.memory) == ["user", "assistant"]


def test_run_result_additive_fields_have_safe_defaults() -> None:
    """RunResult 只加字段不改旧字段顺序：旧式四参构造仍然可用。"""
    from yai_core.types import RunResult

    result = RunResult(Strategy.DIRECT, [], "旧结果")
    assert result.status == "ok" and result.error is None


# ---------- CORE-2 / CORE-4：模型失败 ----------


class _RaisingModel:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        raise self.exc


def test_model_failure_sets_error_status_and_does_not_write_empty_assistant() -> None:
    core = AgentCore(_RaisingModel(RuntimeError("模型服务不可用")))
    core.register_tools([build_spec(search_notes)])

    result = _run(core.run("搜索笔记里的周报"))

    assert result.status == "error"
    assert result.error and "RuntimeError" in result.error
    done = _done(result)
    assert done.data["status"] == "error"
    assert "模型服务不可用" in done.data["error"]
    # 模型故障属于 model 阶段。
    errors = _errors(result)
    assert errors and errors[0].data["stage"] == "model"
    # CORE-4：空 final_text 不写 assistant 历史。
    assert _roles(core.memory) == ["user"]


# ---------- CORE-2：宿主 SPI 故障不得让任务没有终态 ----------


class _BrokenAppendMemory:
    """history 正常、append 必失败：验证收尾不被落盘故障打断。"""

    def __init__(self) -> None:
        self.attempts = 0

    def history(self) -> list[ChatMessage]:
        return []

    async def append_history(self, message: ChatMessage) -> None:
        self.attempts += 1
        raise OSError("disk full")


class _BrokenHistoryMemory:
    """history 必失败、append 正常：验证任务照常使用空历史继续。"""

    def __init__(self) -> None:
        self.appended: list[ChatMessage] = []

    def history(self) -> list[ChatMessage]:
        raise OSError("history unreadable")

    async def append_history(self, message: ChatMessage) -> None:
        self.appended.append(message)


def test_memory_append_failure_still_emits_done() -> None:
    diagnostics: list[Any] = []
    memory = _BrokenAppendMemory()
    core = AgentCore(
        ScriptedModel([ModelResponse(content="完成")]),
        memory=memory,
        on_diagnostic=diagnostics.append,
    )

    result = _run(core.run("你好"))

    assert result.final_text == "完成"
    assert result.status == "ok"  # 任务本身成功，落盘失败只是降级
    assert _done(result).data["status"] == "ok"
    assert memory.attempts >= 1
    assert any(item.kind == "memory_append_failed" for item in diagnostics)


def test_memory_history_failure_degrades_to_empty_history() -> None:
    diagnostics: list[Any] = []
    memory = _BrokenHistoryMemory()
    core = AgentCore(
        ScriptedModel([ModelResponse(content="完成")]),
        memory=memory,
        on_diagnostic=diagnostics.append,
    )

    result = _run(core.run("你好"))

    assert result.final_text == "完成"
    assert _done(result).type == EventType.DONE
    assert [message.role for message in memory.appended] == ["user", "assistant"]
    assert any(item.kind == "memory_history_failed" for item in diagnostics)


class _ClarifyRouter(AdaptiveRouter):
    """无论任务文本都判 clarify，便于打桩通道故障路径。"""

    async def aclassify(self, task, registry, **_kwargs):
        return RouteDecision(Strategy.CLARIFY, "rules", "测试桩：恒澄清")

    def classify(self, task, registry):
        return Strategy.CLARIFY


class _BrokenAskChannel(CollectChannel):
    async def ask(self, question: str) -> str:
        raise ConnectionError("ui disconnected")


def test_channel_ask_failure_terminates_with_error_done() -> None:
    diagnostics: list[Any] = []
    core = AgentCore(
        ScriptedModel([ModelResponse(content="不应被调用")]),
        channel=_BrokenAskChannel(),
        router=_ClarifyRouter(),
        on_diagnostic=diagnostics.append,
    )

    result = _run(core.run("随便"))

    done = _done(result)
    assert done.data["status"] == "error"
    assert done.data["error"] == "clarify_aborted"
    assert "已停止" in result.final_text
    assert result.status == "error"
    assert any(item.kind == "clarify_channel_failed" for item in diagnostics)


def test_clarify_abort_done_carries_status_error() -> None:
    core = AgentCore(
        ScriptedModel([ModelResponse(content="不应被调用")]),
        channel=CollectChannel(default_answer=""),
    )

    result = _run(core.run("随便"))

    done = _done(result)
    assert done.data["status"] == "error"
    assert done.data["error"] == "clarify_aborted"
    assert [event.type.value for event in result.events].count("done") == 1


class _BoomSelector:
    """学习器状态损坏：suggest 抛错的宿主 SPI。"""

    def suggest(self, task, registry):
        raise RuntimeError("selector state corrupt")

    def record(self, *args, **kwargs):  # pragma: no cover - 不会走到
        raise AssertionError("不应记录")


def test_selector_failure_synthesizes_internal_error_and_done() -> None:
    core = AgentCore(
        ScriptedModel([ModelResponse(content="不应被调用")]), learning=_BoomSelector()
    )
    core.register_tools([build_spec(search_notes)])

    result = _run(core.run("搜索笔记里的周报"))

    assert result.status == "error"
    assert result.error and "selector state corrupt" in result.error
    types = [event.type for event in result.events]
    assert types == [EventType.ERROR, EventType.DONE]
    # F5：宿主 SPI 故障不得错报成模型故障。
    assert result.events[0].data["stage"] == "internal"
    assert _done(result).data["status"] == "error"
    # 失败在路由阶段：没有历史写入。
    assert _roles(core.memory) == []


# ---------- F6：限尾收尾仍要调工具 ----------


def test_iteration_limit_with_tool_calls_emits_error_and_honest_text() -> None:
    tc = ToolCallRequest(id="c1", name="search_notes", arguments={"keyword": "周报"})
    model = ScriptedModel([ModelResponse(content="", tool_calls=[tc])])
    core = AgentCore(model, max_iters=1)
    core.register_tools([build_spec(search_notes)])

    result = _run(core.run("搜索笔记里的周报"))

    errors = _errors(result)
    assert any(event.data.get("stage") == "iteration_limit" for event in errors)
    messages = [e for e in result.events if e.type == EventType.MODEL_MESSAGE]
    assert messages and messages[-1].data["text"].strip()
    assert result.final_text.strip()
    assert result.status == "error"
    assert _done(result).data["status"] == "error"
    # 诚实文案进入历史（非空），但不再有"空 assistant"。
    assert _roles(core.memory) == ["user", "assistant"]


# ---------- F9：主动召回超时 ----------


def test_auto_recall_timeout_degrades_without_blocking() -> None:
    diagnostics: list[Any] = []
    core = AgentCore(
        ScriptedModel([ModelResponse(content="完成")]),
        model_timeout=0.05,
        on_diagnostic=diagnostics.append,
    )

    async def slow_recall(task: str):
        await asyncio.sleep(1.0)
        return [{"id": "1", "kind": "note", "content": "迟到的记忆"}]

    core._loop._auto_recall = slow_recall  # noqa: SLF001 - 直接验证召回超时兜底

    result = _run(core.run("你好"))

    assert result.final_text == "完成"
    assert not [
        event for event in result.events if event.type == EventType.MEMORIES_RECALLED
    ]
    assert any(item.kind == "auto_recall_timeout" for item in diagnostics)


# ---------- 取消：传播且不发 DONE ----------


class _BlockingModel:
    async def achat(self, messages, tools=None, *, tier="standard"):
        await asyncio.Event().wait()  # 永远挂起，等外层取消


def test_cancel_propagates_without_done_and_releases_lock() -> None:
    async def scenario() -> None:
        core = AgentCore(_BlockingModel())
        seen = []

        async def consume() -> None:
            async for event in core.astream("你好"):
                seen.append(event)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not any(event.type == EventType.DONE for event in seen)
        assert _roles(core.memory) == []  # 取消不落历史

        # 锁已释放：换一个可用的模型后同一实例可以继续跑。
        core._loop.model = ScriptedModel([ModelResponse(content="继续可用")])
        result = await core.run("再来一次")
        assert result.final_text == "继续可用"

    _run(scenario())


# ---------- F11：单 run 收据链可独立校验 ----------


class _ReactOnceModel:
    """每轮先调一次工具、拿到工具观察后收尾：便于观察 run 级收据链重置。"""

    async def achat(self, messages, tools=None, *, tier="standard"):
        if any(isinstance(m, dict) and m.get("role") == "tool" for m in (messages or [])):
            return ModelResponse(content="本轮完成。")
        return _tool_calls()


def test_each_run_receipt_chain_verifies_independently() -> None:
    core = AgentCore(_ReactOnceModel())
    core.register_tools([build_spec(search_notes)])

    def _receipts(events) -> list[dict]:
        out: list[dict] = []
        for event in events:
            if event.type == EventType.TOOL_RESULT and isinstance(
                event.data.get("receipt"), dict
            ):
                out.append(event.data["receipt"])
        return out

    first = _run(core.run("搜索笔记里的周报"))
    second = _run(core.run("再搜索一次周报"))

    first_receipts = _receipts(first.events)
    second_receipts = _receipts(second.events)
    assert first_receipts and second_receipts
    assert verify_receipt_chain(first_receipts) is True
    assert verify_receipt_chain(second_receipts) is True
    # 每个 run 的链首都不再挂着上一任务的哈希。
    assert first_receipts[0]["prev_hash"] == ""
    assert second_receipts[0]["prev_hash"] == ""
