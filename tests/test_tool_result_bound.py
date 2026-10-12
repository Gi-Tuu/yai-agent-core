"""C4 工具结果统一有界收口测试（纯标准库 + 离线，不需要 API Key）。

覆盖：
- ``bound_tool_result`` 纯函数：未超限原样、超限头+尾保留且严格不越上界、None 关闭；
- ``ToolExecutor`` 三类结果在回灌进上下文前统一收口，TOOL_RESULT 事件带
  ``truncated`` / ``result_chars`` 标记；
- 默认阈值下小结果逐字不变（向后兼容）。
"""

import asyncio
import json

import pytest

from yai_core.channels import CollectChannel
from yai_core.policy import AllowlistPolicy
from yai_core.tools.executor import (
    DEFAULT_TOOL_RESULT_CHARS,
    ToolExecutor,
    bound_tool_result,
)
from yai_core.tools.registry import ToolRegistry
from yai_core.types import EventType, ToolSpec


def _make_executor(limit: int | None) -> tuple[ToolExecutor, ToolRegistry]:
    reg = ToolRegistry()
    policy = AllowlistPolicy(mode="allow_all")
    channel = CollectChannel()
    ex = ToolExecutor(reg, policy, channel, result_char_limit=limit)
    return ex, reg


def _big_spec(name: str = "big", size: int = 40_000) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="返回超长文本",
        input_schema={"type": "object", "properties": {}},
        handler=lambda: "数" * size,
    )


# ---------- bound_tool_result 纯函数 ----------


def test_bound_under_limit_returns_unchanged() -> None:
    out, truncated, original = bound_tool_result("abc", 10)
    assert out == "abc"
    assert truncated is False
    assert original == 3


def test_bound_none_limit_disables() -> None:
    out, truncated, original = bound_tool_result("abc", None)
    assert out == "abc"
    assert truncated is False
    assert original == 3


def test_bound_at_exact_limit_not_truncated() -> None:
    s = "x" * 100
    out, truncated, original = bound_tool_result(s, 100)
    assert out == s
    assert truncated is False
    assert original == 100


def test_bound_over_limit_keeps_head_and_tail_within_bound() -> None:
    s = "x" * 1000
    out, truncated, original = bound_tool_result(s, 100)
    assert truncated is True
    assert original == 1000
    assert len(out) <= 100
    assert "已省略" in out
    assert out.startswith("x")
    assert out.endswith("x")


def test_bound_never_exceeds_limit_across_sizes() -> None:
    for size in (121, 257, 999, 5000):
        out, truncated, original = bound_tool_result("y" * size, 120)
        assert len(out) <= 120
        assert truncated is True
        assert original == size


# ---------- ToolExecutor 集成 ----------


def test_executor_native_large_result_is_bounded_and_flagged() -> None:
    ex, reg = _make_executor(1000)
    reg.register(_big_spec())
    raw_len = len(json.dumps("数" * 40_000, ensure_ascii=False, default=str))

    events, ok, text = asyncio.run(ex.execute("big", {}))

    assert ok is True
    assert len(text) <= 1000
    assert "已省略" in text
    # json 字符串头尾引号都被保留：头部在、尾部在。
    assert text.startswith('"')
    assert text.endswith('"')
    flagged = [
        e.data
        for e in events
        if e.type == EventType.TOOL_RESULT and e.data.get("ok")
    ]
    assert flagged[-1]["truncated"] is True
    assert flagged[-1]["result_chars"] == raw_len
    assert len(flagged[-1]["preview"]) <= 200


def test_executor_none_limit_keeps_full_result() -> None:
    ex, reg = _make_executor(None)
    reg.register(_big_spec())
    raw_len = len(json.dumps("数" * 40_000, ensure_ascii=False, default=str))

    events, ok, text = asyncio.run(ex.execute("big", {}))

    assert ok is True
    assert len(text) == raw_len
    flagged = [
        e.data for e in events if e.type == EventType.TOOL_RESULT and e.data.get("ok")
    ]
    assert flagged[-1]["truncated"] is False
    assert flagged[-1]["result_chars"] == raw_len


def test_executor_small_result_unchanged_under_default() -> None:
    ex, reg = _make_executor(DEFAULT_TOOL_RESULT_CHARS)
    reg.register(
        ToolSpec(
            name="small",
            description="返回小对象",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: {"ok": True},
        )
    )
    expected = json.dumps({"ok": True}, ensure_ascii=False, default=str)

    events, ok, text = asyncio.run(ex.execute("small", {}))

    assert ok is True
    assert text == expected
    flagged = [
        e.data for e in events if e.type == EventType.TOOL_RESULT and e.data.get("ok")
    ]
    assert flagged[-1]["truncated"] is False
    assert flagged[-1]["result_chars"] == len(expected)


def test_executor_rejects_invalid_limit() -> None:
    reg = ToolRegistry()
    policy = AllowlistPolicy(mode="allow_all")
    channel = CollectChannel()
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            ToolExecutor(reg, policy, channel, result_char_limit=bad)  # type: ignore[arg-type]
