"""离线测试：上下文摘要压缩（select 边界 / 摘要替换 / 失败回退 / loop 集成）。

全部用 ScriptedModel，不需要 API Key、不访问网络。
"""

import asyncio

import pytest

from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec
from yai_core.kernel import CompactionPolicy
from yai_core.kernel.compaction import (
    compact_context,
    render_for_summary,
    select_compaction_range,
    user_round_starts,
)
from yai_core.kernel.context import Context
from yai_core.memory import InMemoryStore
from yai_core.types import ChatMessage, EventType


class ScriptedModel:
    """按预设顺序返回响应的假模型；raises=True 时模拟模型异常。"""

    def __init__(self, responses=None, *, raises=False) -> None:
        self._responses = responses or []
        self._raises = raises
        self.calls = 0
        self.requests: list[list[dict]] = []

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        self.requests.append([dict(message) for message in messages])
        if self._raises:
            raise RuntimeError("model boom")
        if not self._responses:
            return ModelResponse(content="")
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]


def _plain_round(prefix: str, *, n: int = 120) -> list[ChatMessage]:
    """一轮普通对话（user + assistant），内容足够长以触发低水位。"""
    return [
        ChatMessage(role="user", content=f"{prefix}-用户-" + "U" * n),
        ChatMessage(role="assistant", content=f"{prefix}-助手-" + "A" * n),
    ]


# ---------- select_compaction_range：纯函数边界 ----------

def test_select_none_under_limit() -> None:
    msgs = [
        ChatMessage(role="system", content="s"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="yo"),
    ]
    assert select_compaction_range(msgs, CompactionPolicy()) is None


def test_select_none_when_too_few_rounds() -> None:
    msgs = [ChatMessage(role="system", content="s")]
    msgs += _plain_round("r1")
    msgs += _plain_round("r2")
    policy = CompactionPolicy(hard_max_chars=10, keep_recent_rounds=2)
    # 仅 2 轮、且都要完整保留 → 无法压缩。
    assert select_compaction_range(msgs, policy) is None


def test_select_range_keeps_last_n_rounds() -> None:
    msgs = [ChatMessage(role="system", content="s")]
    for i in range(4):
        msgs += _plain_round(f"r{i}")
    policy = CompactionPolicy(hard_max_chars=10, keep_recent_rounds=2)
    rng = select_compaction_range(msgs, policy)
    assert rng is not None
    start, end = rng
    # 边界都对齐 user 轮首，绝不从轮中间切。
    assert msgs[start].role == "user"
    assert msgs[end].role == "user"
    starts = user_round_starts(msgs)
    assert start == starts[0]
    assert end == starts[-2]  # 保留最后 2 个完整轮


def test_select_does_not_split_tool_pairs() -> None:
    msgs = [ChatMessage(role="system", content="s")]
    # 旧轮内含 assistant(tool_calls) + 对应 tool 结果。
    msgs += [
        ChatMessage(role="user", content="old1 " + "U" * 100),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }
            ],
        ),
        ChatMessage(
            role="tool",
            content="result " + "R" * 100,
            tool_call_id="t1",
            name="search",
        ),
        ChatMessage(role="assistant", content="old1 done " + "A" * 80),
    ]
    msgs += _plain_round("mid")
    msgs += _plain_round("recent")
    policy = CompactionPolicy(hard_max_chars=10, keep_recent_rounds=1)
    rng = select_compaction_range(msgs, policy)
    assert rng is not None
    start, end = rng
    assert msgs[end].role == "user"
    # 压缩区间内每个 tool_calls 的对应 tool 消息也在区间内（一起进摘要，不孤立）。
    for i in range(start, end):
        if msgs[i].tool_calls:
            call_ids = {tc["id"] for tc in msgs[i].tool_calls}
            tool_ids = {
                msgs[j].tool_call_id
                for j in range(i + 1, end)
                if msgs[j].role == "tool"
            }
            assert call_ids <= tool_ids


# ---------- render_for_summary ----------

def test_render_includes_facts_and_tool() -> None:
    msgs = [
        ChatMessage(role="user", content="查天气"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        ),
        ChatMessage(
            role="tool",
            content="晴 28℃",
            tool_call_id="t1",
            name="get_weather",
        ),
    ]
    text = render_for_summary(msgs, 0, 3)
    assert "查天气" in text
    assert "get_weather" in text
    assert "晴 28℃" in text


# ---------- compact_context：摘要替换与回退 ----------

def _long_context(n_rounds: int = 4) -> Context:
    # 大 max_chars：add 不触发硬裁剪；压缩完全由独立 policy 触发。
    ctx = Context("系统提示", max_chars=1_000_000)
    for i in range(n_rounds):
        for msg in _plain_round(f"r{i}"):
            ctx.add(msg)
    return ctx


def test_compact_none_under_limit_and_no_model_call() -> None:
    ctx = Context("s")
    ctx.add(ChatMessage(role="user", content="hi"))
    model = ScriptedModel([ModelResponse(content="x")])
    assert asyncio.run(compact_context(ctx, model, CompactionPolicy())) is None
    assert model.calls == 0


def test_compact_replaces_old_keeps_recent() -> None:
    ctx = _long_context(4)
    model = ScriptedModel([ModelResponse(content="这是压缩摘要")])
    policy = CompactionPolicy(hard_max_chars=10, keep_recent_rounds=2)
    ev = asyncio.run(compact_context(ctx, model, policy))
    assert ev is not None
    assert ev.type == EventType.CONTEXT_COMPACTED
    # 原 system 仍在首位。
    assert ctx.messages[0].content == "系统提示"
    joined = "\n".join(m.content for m in ctx.messages)
    assert "这是压缩摘要" in joined
    # 最近 2 轮完整保留，旧 2 轮被替换。
    assert "r2" in joined and "r3" in joined
    assert "r0" not in joined and "r1" not in joined
    # system + 一条摘要 + 最近 2 轮（4 条）。
    assert len(ctx.messages) == 6
    assert ev.data["summary_chars"] == len("这是压缩摘要")
    assert ev.data["kept_recent_rounds"] == 2
    assert ev.data["compacted_messages"] == 4
    assert ev.data["compacted_chars"] > 0


def test_compact_model_error_falls_back_hard_trim() -> None:
    ctx = Context("s", max_chars=260)
    for i in range(4):  # 直接拼 messages，绕过 add 的即时裁剪。
        ctx.messages += _plain_round(f"r{i}")
    assert ctx.total_chars() > 260
    model = ScriptedModel(raises=True)
    policy = CompactionPolicy(hard_max_chars=10, keep_recent_rounds=2)
    before = len(ctx.messages)
    ev = asyncio.run(compact_context(ctx, model, policy))
    assert ev is None
    assert len(ctx.messages) < before  # 硬裁剪已兜底执行


def test_compact_empty_summary_falls_back_hard_trim() -> None:
    ctx = Context("s", max_chars=260)
    for i in range(4):
        ctx.messages += _plain_round(f"r{i}")
    model = ScriptedModel([ModelResponse(content="   ")])
    policy = CompactionPolicy(hard_max_chars=10, keep_recent_rounds=2)
    ev = asyncio.run(compact_context(ctx, model, policy))
    assert ev is None


# ---------- loop 集成：长历史触发压缩并正常收尾 ----------

def test_loop_emits_context_compacted_for_long_history() -> None:
    async def scenario():
        memory = InMemoryStore()
        for i in range(3):
            await memory.append_history(
                ChatMessage(role="user", content=f"旧任务{i} " + "U" * 80)
            )
            await memory.append_history(
                ChatMessage(role="assistant", content=f"旧答复{i} " + "A" * 80)
            )
        # 调用顺序：① 摘要压缩 ② 主任务最终答复。
        model = ScriptedModel(
            [
                ModelResponse(content="历史摘要XYZ"),
                ModelResponse(content="这是最终答复"),
            ]
        )
        policy = CompactionPolicy(hard_max_chars=300, keep_recent_rounds=1)
        core = AgentCore(model, memory=memory, context_compaction=policy)
        return await core.run("你好")

    result = asyncio.run(scenario())
    types = [e.type for e in result.events]
    assert EventType.CONTEXT_COMPACTED in types
    assert types[-1] == EventType.DONE
    assert result.final_text == "这是最终答复"


def test_loop_compaction_uses_dedicated_compaction_model() -> None:
    """C2：注入 compaction_model 后，摘要走专用小模型，主模型只做最终答复。"""

    async def scenario():
        memory = InMemoryStore()
        for i in range(3):
            await memory.append_history(
                ChatMessage(role="user", content=f"旧任务{i} " + "U" * 80)
            )
            await memory.append_history(
                ChatMessage(role="assistant", content=f"旧答复{i} " + "A" * 80)
            )
        summary_model = ScriptedModel([ModelResponse(content="专用模型摘要QWE")])
        main_model = ScriptedModel([ModelResponse(content="主模型最终答复")])
        policy = CompactionPolicy(hard_max_chars=300, keep_recent_rounds=1)
        core = AgentCore(
            main_model,
            memory=memory,
            context_compaction=policy,
            compaction_model=summary_model,
        )
        result = await core.run("你好")
        return result, main_model, summary_model

    result, main_model, summary_model = asyncio.run(scenario())
    types = [e.type for e in result.events]
    assert EventType.CONTEXT_COMPACTED in types
    assert result.final_text == "主模型最终答复"
    # 压缩恰好调用一次专用模型；主模型只被调一次（最终答复），没被借去做摘要。
    assert summary_model.calls == 1
    assert main_model.calls == 1


# ---------- CompactionPolicy.from_context_window：按比例水位工厂（C3） ----------


def test_from_context_window_default_ratios_match_defaults() -> None:
    # 40000 字符工作区 × 0.6 / 0.4 恰好等于无参默认 24000 / 16000（换算锚点）。
    assert CompactionPolicy.from_context_window(40_000) == CompactionPolicy()


def test_from_context_window_custom_ratios_and_rounds() -> None:
    p = CompactionPolicy.from_context_window(
        100_000, keep_recent_rounds=3, hard_ratio=0.5, target_ratio=0.25
    )
    assert p.hard_max_chars == 50_000
    assert p.target_chars == 25_000
    assert p.keep_recent_rounds == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chars": 0},
        {"chars": -1},
        {"chars": True},
        {"chars": 100_000, "hard_ratio": 0.4, "target_ratio": 0.5},  # 高低倒挂
        {"chars": 100_000, "hard_ratio": 0.5, "target_ratio": 0.5},  # 高低相等
        {"chars": 100_000, "hard_ratio": 1.2, "target_ratio": 0.4},  # 高水位 >1
        {"chars": 100_000, "target_ratio": 0.0},  # 低水位 0
        {"chars": 100_000, "keep_recent_rounds": 0},
        {"chars": 1},  # 窗口过小，按比例取整后高/低水位为 0
    ],
)
def test_from_context_window_rejects_invalid(kwargs) -> None:
    with pytest.raises(ValueError):
        CompactionPolicy.from_context_window(**kwargs)


# ---------- 先压缩、后硬裁剪：默认水位也要能触发（A3） ----------


async def _long_memory(rounds: int = 4) -> InMemoryStore:
    """4 轮 × 约 8000 字符，超过默认 24000 水位；首轮带可辨认事实。"""
    memory = InMemoryStore()
    await memory.append_history(
        ChatMessage(role="user", content="KEEP0 关键约束 " + "U" * 4000)
    )
    await memory.append_history(
        ChatMessage(role="assistant", content="EARLY_ASSISTANT 已记录 " + "A" * 4000)
    )
    for i in range(1, rounds):
        for msg in _plain_round(f"r{i}", n=4000):
            await memory.append_history(msg)
    return memory


def test_default_policy_compacts_before_hard_trim() -> None:
    async def scenario():
        model = ScriptedModel(
            [
                ModelResponse(content="摘要保留EARLY要点"),
                ModelResponse(content="最终答复"),
            ]
        )
        core = AgentCore(model, memory=await _long_memory(), context_compaction=True)
        return await core.run("继续处理"), model

    result, model = asyncio.run(scenario())
    assert EventType.CONTEXT_COMPACTED in [e.type for e in result.events]
    # 摘要模型看到最早一轮，而不是等它被 add 的即时硬裁剪删掉。
    summary_input = "\n".join(str(m.get("content", "")) for m in model.requests[0])
    assert "KEEP0" in summary_input and "EARLY_ASSISTANT" in summary_input
    # 主调用拿到摘要，最早一轮不再逐字出现。
    main_input = "\n".join(str(m.get("content", "")) for m in model.requests[-1])
    assert "摘要保留EARLY要点" in main_input
    assert "EARLY_ASSISTANT" not in main_input
    assert result.final_text == "最终答复"


def test_plan_compacts_before_planning_model_call() -> None:
    def search_notes(keyword: str) -> list:
        """搜索笔记。"""
        return []

    async def scenario():
        model = ScriptedModel(
            [
                ModelResponse(content="摘要保留EARLY要点"),
                ModelResponse(content="1. 搜索笔记\n2. 汇报"),
                ModelResponse(content="完成。"),
            ]
        )
        core = AgentCore(model, memory=await _long_memory(), context_compaction=True)
        core.register_tools([build_spec(search_notes)])
        return await core.run("先搜索笔记，然后汇报"), model

    result, model = asyncio.run(scenario())
    types = [e.type for e in result.events]
    assert types.index(EventType.CONTEXT_COMPACTED) < types.index(EventType.PLAN_CREATED)
    assert "EARLY_ASSISTANT" in "\n".join(
        str(m.get("content", "")) for m in model.requests[0]
    )
    plan_input = "\n".join(str(m.get("content", "")) for m in model.requests[1])
    assert "摘要保留EARLY要点" in plan_input
    assert "EARLY_ASSISTANT" not in plan_input


def test_policy_watermark_above_context_cap_still_compacts() -> None:
    """宿主把水位设得比 Context 硬上限还高时，仍要在硬裁剪之前触发压缩。"""

    async def scenario():
        model = ScriptedModel(
            [
                ModelResponse(content="摘要保留EARLY要点"),
                ModelResponse(content="最终答复"),
            ]
        )
        policy = CompactionPolicy(hard_max_chars=100_000, keep_recent_rounds=2)
        core = AgentCore(model, memory=await _long_memory(), context_compaction=policy)
        return await core.run("继续处理")

    result = asyncio.run(scenario())
    assert EventType.CONTEXT_COMPACTED in [e.type for e in result.events]


def test_large_tool_result_does_not_trim_before_compaction() -> None:
    def big() -> str:
        """返回大段内容。"""
        return "B" * 15_000

    async def scenario():
        memory = InMemoryStore()
        await memory.append_history(
            ChatMessage(role="user", content="KEEP0 早期任务 " + "U" * 3000)
        )
        await memory.append_history(
            ChatMessage(role="assistant", content="EARLY_ASSISTANT 收到 " + "A" * 3000)
        )
        for msg in _plain_round("r1", n=3000):
            await memory.append_history(msg)
        model = ScriptedModel(
            [
                ModelResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="t1", name="big", arguments={})],
                ),
                ModelResponse(content="摘要保留EARLY要点"),
                ModelResponse(content="最终答复"),
            ]
        )
        core = AgentCore(model, memory=memory, context_compaction=True)
        core.register_tools([build_spec(big)])
        return await core.run("搜索笔记里的比赛内容"), model

    result, model = asyncio.run(scenario())
    assert EventType.CONTEXT_COMPACTED in [e.type for e in result.events]
    # 工具结果把总量推过水位后，压缩器仍能看到最早一轮。
    assert "EARLY_ASSISTANT" in "\n".join(
        str(m.get("content", "")) for m in model.requests[1]
    )
