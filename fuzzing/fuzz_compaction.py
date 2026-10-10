"""Fuzz target：上下文压缩的纯函数区间选择与渲染.

覆盖 :func:`user_round_starts`、:func:`select_compaction_range`、
:func:`render_for_summary`。长任务里消息序列的轮次边界、recent 窗口对齐、
工具配对不切断都靠这几个函数，喂随机（但结构合法）的消息序列验证其稳健。

``compact_context`` 是异步且需要模型的编排函数，不在此 fuzz；它的离线行为由
``tests/`` 中的 ScriptedModel 用例覆盖。

平台/运行方式同 :mod:`fuzz_features`：

    uv run python fuzzing/fuzz_compaction.py -max_total_time=45
"""

from __future__ import annotations

import sys

import atheris

from yai_core.kernel.compaction import (
    CompactionPolicy,
    render_for_summary,
    select_compaction_range,
    user_round_starts,
)
from yai_core.types import ChatMessage

_ROLES = ("system", "user", "assistant", "tool")


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)

    n = fdp.ConsumeIntInRange(0, 12)
    messages: list[ChatMessage] = []
    for _ in range(n):
        role = _ROLES[fdp.ConsumeIntInRange(0, len(_ROLES) - 1)]
        content = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 200))
        tool_calls = None
        if role == "assistant" and fdp.ConsumeBool():
            tool_calls = [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": fdp.ConsumeUnicodeNoSurrogates(8),
                        "arguments": "{}",
                    },
                }
            ]
        messages.append(ChatMessage(role=role, content=content, tool_calls=tool_calls))

    starts = user_round_starts(messages)
    assert all(1 <= idx < len(messages) for idx in starts)

    policy = CompactionPolicy(
        hard_max_chars=fdp.ConsumeIntInRange(0, 5000),
        target_chars=fdp.ConsumeIntInRange(0, 5000),
        keep_recent_rounds=fdp.ConsumeIntInRange(0, 5),
    )
    rng = select_compaction_range(messages, policy)
    if rng is not None:
        start, end = rng
        assert 0 <= start < end <= len(messages)
        rendered = render_for_summary(messages, start, end)
        assert isinstance(rendered, str)


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
