"""上下文摘要压缩：超预算时把旧轮压成一条摘要，最近完整轮原样保留。

与硬裁剪（``Context.enforce_budget``，直接丢弃、信息彻底丢失）不同，摘要压缩
保留旧历史的要点，适合长任务 / 长期运行的灵动岛，避免模型忘记早期目标与事实。
它需要一次额外模型调用，失败或摘要为空时自动回退硬裁剪。

纯标准库、零硬依赖；全部行为可离线测试（ScriptedModel 返回预设摘要）。
"""

from __future__ import annotations

from dataclasses import dataclass

from yai_core.kernel.context import Context
from yai_core.types import AgentEvent, ChatMessage, EventType

#: 摘要器的系统指令：只保留对后续任务有用的信息，去掉寒暄与重复。
_SUMMARY_SYSTEM = (
    "你是对话摘要器。把给定的对话历史压缩成简洁的中文摘要，务必保留："
    "用户目标与关键约束、已确认的事实与数据、已完成的工具调用及其关键结果、"
    "尚未解决的问题；去掉寒暄与重复。只输出摘要本身，不要分点标题。"
)


@dataclass(frozen=True)
class CompactionPolicy:
    """上下文压缩策略。

    - hard_max_chars：触发压缩的高水位（与 ``Context.max_chars`` 同口径）；
    - target_chars：压缩后期望的低水位（recent 窗口必须完整时可能达不到，仅作口径）；
    - keep_recent_rounds：最近多少个完整轮（以 user 轮首起算）原样保留、不进摘要。
    """

    hard_max_chars: int = 24000
    target_chars: int = 16000
    keep_recent_rounds: int = 2


def user_round_starts(messages: list[ChatMessage]) -> list[int]:
    """返回每个 user 轮首的索引（跳过 index 0 的 system）。"""
    return [i for i in range(1, len(messages)) if messages[i].role == "user"]


def select_compaction_range(
    messages: list[ChatMessage], policy: CompactionPolicy
) -> tuple[int, int] | None:
    """确定要压成摘要的旧消息区间 ``[start, end)``；不需要 / 无法压缩返回 None。

    规则：
    - 总字符未达高水位 → None；
    - 对齐 user 轮首，保留最后 ``keep_recent_rounds`` 个完整轮，其之前的完整旧轮
      进摘要（绝不切断 assistant(tool_calls)/tool 配对）；
    - 完整旧轮数不足（所有非 system 消息都落在 recent 窗口内）→ None，
      交给硬裁剪兜底。
    """
    total = sum(len(m.content) for m in messages)
    if total <= policy.hard_max_chars:
        return None
    starts = user_round_starts(messages)
    if len(starts) <= policy.keep_recent_rounds:
        return None
    start = starts[0]
    keep_from = starts[-policy.keep_recent_rounds]
    if keep_from <= start:
        return None
    return (start, keep_from)


def render_for_summary(
    messages: list[ChatMessage], start: int, end: int
) -> str:
    """把 ``[start, end)`` 的消息渲染成供摘要的纯文本（含工具结果，保留事实）。"""
    lines: list[str] = []
    for m in messages[start:end]:
        if m.role == "user":
            lines.append(f"用户：{m.content}")
        elif m.role == "assistant":
            text = m.content or ""
            if m.tool_calls:
                names = ", ".join(
                    str(tc.get("function", {}).get("name", "?")) for tc in m.tool_calls
                )
                text = (text + f"\n[调用工具：{names}]").strip()
            lines.append(f"助手：{text}")
        elif m.role == "tool":
            lines.append(f"工具结果：{m.content}")
        else:
            lines.append(m.content)
    return "\n".join(lines)


async def compact_context(
    ctx: Context, model: object, policy: CompactionPolicy
) -> AgentEvent | None:
    """超预算时把旧轮摘要压缩；返回 ``CONTEXT_COMPACTED`` 事件，否则返回 None。

    成功：旧轮 → 一条 system 摘要，recent 完整轮原样保留，结构对模型合法。
    模型异常或返回空摘要：回退硬裁剪（``enforce_budget``），不抛错、不破坏结构。
    """
    rng = select_compaction_range(ctx.messages, policy)
    if rng is None:
        return None
    start, end = rng
    before_msgs = end - start
    before_chars = sum(len(m.content) for m in ctx.messages[start:end])
    history_text = render_for_summary(ctx.messages, start, end)
    try:
        resp = await model.achat(  # type: ignore[attr-defined]
            [
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": history_text},
            ],
            tools=None,
            tier="standard",
        )
        summary = (getattr(resp, "content", "") or "").strip()
    except Exception:  # noqa: BLE001 - 摘要是增强，失败回退硬裁剪
        ctx.enforce_budget()
        return None
    if not summary:
        ctx.enforce_budget()
        return None

    summary_msg = ChatMessage(role="system", content=f"【早期对话摘要】\n{summary}")
    ctx.replace_range(start, end, summary_msg)
    return AgentEvent(
        EventType.CONTEXT_COMPACTED,
        {
            "compacted_messages": before_msgs,
            "compacted_chars": before_chars,
            "summary_chars": len(summary),
            "kept_recent_rounds": policy.keep_recent_rounds,
            "total_chars_after": ctx.total_chars(),
        },
    )
