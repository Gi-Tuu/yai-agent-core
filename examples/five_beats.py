"""第三方播种的"五拍"标准线：把一次成功的自适应接管拆成可观测节拍。

无论宿主是 Actual Budget、Mealie 还是任意真实应用，YAI 嵌入后一次完整的
"长尾任务接管"都应能在事件流里看到下面五个转折（授权与补能力常同拍发生，
因此落地为 6 个可观测事件点）：

1. 唤醒      ``STRATEGY_SELECTED``        内核接住用户目标、选定策略
2. 长尾任务  多个不同 ``TOOL_CALL``       一个目标串起多步、跨越单轮问答
3. 缺口      ``CAPABILITY_MISSING``       内核识别现有工具不足以完成
4. 补能力/派子员工  ``TOOL_DISCOVERED``   按需发现新工具
                  或 ``TOOL_CALL(delegate)`` 派短命子员工并行处理
5. 授权      ``PERMISSION_ASKED``         新工具/写操作经权限闸向用户确认
6. 完成      ``DONE``                     根对用户给出唯一最终结果

两条典型链路：

- **发现链路**（:func:`assert_discover_beats`）：缺口 → 发现工具 → 授权 → 完成。
- **委派链路**（:func:`assert_delegate_beats`）：缺口表现为"需并行的多个独立
  子目标" → 根派只读子员工（白名单收窄、最小授权）→ 结果回流 → 完成。

本模块只用标准库 + 内核事件类型，供各宿主离线测试复用，作为"五拍"的可执行
定义，而不是停留在 README 里的口号。
"""

from __future__ import annotations

from yai_core.types import AgentEvent, EventType

#: 节拍名 → 事件类型（delegate 是工具调用，需要额外判断工具名）。
WAKE = "wake"
LONG_TAIL = "long_tail"
GAP = "gap"
DISCOVER = "discover"
DELEGATE = "delegate"
AUTHORIZE = "authorize"
DONE = "done"


def _tool_name(event: AgentEvent) -> str | None:
    if event.type in (EventType.TOOL_CALL, EventType.PERMISSION_ASKED):
        return str(event.data.get("tool"))
    return None


def beat_timeline(events: list[AgentEvent]) -> list[tuple[str, int]]:
    """把事件流压缩成有序 ``(节拍, 事件下标)`` 列表（同节拍只留首次）。"""
    timeline: list[tuple[str, int]] = []
    seen: set[str] = set()
    distinct_tools: set[str] = set()

    def mark(beat: str, index: int) -> None:
        if beat not in seen:
            timeline.append((beat, index))
            seen.add(beat)

    for i, event in enumerate(events):
        if event.type == EventType.STRATEGY_SELECTED:
            mark(WAKE, i)
        elif event.type == EventType.TOOL_CALL:
            name = _tool_name(event) or ""
            distinct_tools.add(name)
            if name == "delegate":
                mark(DELEGATE, i)
            if len(distinct_tools) >= 2:
                mark(LONG_TAIL, i)
        elif event.type == EventType.CAPABILITY_MISSING:
            mark(GAP, i)
        elif event.type == EventType.TOOL_DISCOVERED:
            mark(DISCOVER, i)
        elif event.type == EventType.PERMISSION_ASKED:
            mark(AUTHORIZE, i)
        elif event.type == EventType.DONE:
            mark(DONE, i)
    return timeline


def present_beats(events: list[AgentEvent]) -> set[str]:
    """返回事件流中出现过的节拍集合。"""
    return {beat for beat, _ in beat_timeline(events)}


def _index_of(timeline: list[tuple[str, int]], beat: str) -> int | None:
    for b, i in timeline:
        if b == beat:
            return i
    return None


def assert_discover_beats(events: list[AgentEvent]) -> set[str]:
    """断言"缺口 → 发现 → 授权 → 完成"链路完整且顺序正确。"""
    timeline = beat_timeline(events)
    have = {b for b, _ in timeline}

    assert WAKE in have, "缺少①唤醒：未看到 STRATEGY_SELECTED"
    assert GAP in have, "缺少③缺口：未看到 CAPABILITY_MISSING"
    assert DISCOVER in have, "缺少④补能力：未看到 TOOL_DISCOVERED"
    assert AUTHORIZE in have, "缺少⑤授权：新工具/写操作未经过 PERMISSION_ASKED"
    assert DONE in have, "缺少⑥完成：未看到 DONE"
    assert LONG_TAIL in have, "缺少②长尾：应至少串联两个不同工具"

    i_gap = _index_of(timeline, GAP)
    i_disc = _index_of(timeline, DISCOVER)
    i_auth = _index_of(timeline, AUTHORIZE)
    i_done = _index_of(timeline, DONE)
    assert i_gap is not None and i_disc is not None and i_gap < i_disc, (
        "顺序错误：能力缺口必须先于工具发现"
    )
    assert i_auth is not None and i_disc is not None and i_auth > i_gap, (
        "顺序错误：授权应发生在发现新能力之后"
    )
    assert i_done is not None and i_done == max(i for _, i in timeline), (
        "DONE 必须是最后一个节拍"
    )
    return have


def assert_delegate_beats(events: list[AgentEvent], *, require_auth: bool = False) -> set[str]:
    """断言"派子员工 → （最小授权）→ 完成"链路完整。

    只读子员工的工具在其白名单内自动放行，因此默认不强制出现 PERMISSION_ASKED；
    若委派中包含写操作，可置 ``require_auth=True``。
    """
    timeline = beat_timeline(events)
    have = {b for b, _ in timeline}

    assert WAKE in have, "缺少①唤醒：未看到 STRATEGY_SELECTED"
    assert DELEGATE in have, "缺少④派子员工：未看到对 delegate 的工具调用"
    assert DONE in have, "缺少⑥完成：未看到 DONE"

    i_delegate = _index_of(timeline, DELEGATE)
    i_done = _index_of(timeline, DONE)
    assert i_delegate is not None and i_done is not None and i_delegate < i_done, (
        "顺序错误：委派必须先于根的最终汇总"
    )
    if require_auth:
        assert AUTHORIZE in have, "要求授权但未看到 PERMISSION_ASKED"
    return have
