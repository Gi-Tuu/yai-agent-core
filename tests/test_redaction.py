"""声明式参数脱敏回归：对外副本看不到密钥，执行仍拿到真值（全程离线）。"""

from __future__ import annotations

import asyncio

from yai_core import build_spec
from yai_core.policy import AllowlistPolicy
from yai_core.policy.redact import redact_arguments
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import EventType, ToolGovernance

SECRET = "sk-live-abcdefghijklmnop"


# ---------- 纯函数 ----------

def test_redacts_nested_and_indexed_paths() -> None:
    payload = {
        "api_key": SECRET,
        "headers": {"Authorization": f"Bearer {SECRET}", "X-Trace": "keep"},
        "rows": [{"token": SECRET}, {"token": "other"}],
    }
    out = redact_arguments(
        payload, ("api_key", "headers.Authorization", "rows.0.token")
    )
    assert out["api_key"] == f"[redacted:len={len(SECRET)}]"
    assert out["headers"]["Authorization"].startswith("[redacted")
    assert out["headers"]["X-Trace"] == "keep"          # 没声明的不动
    assert out["rows"][0]["token"].startswith("[redacted")
    assert out["rows"][1]["token"] == "other"
    assert payload["api_key"] == SECRET                  # 原对象不被污染


def test_missing_or_bogus_paths_are_skipped() -> None:
    out = redact_arguments(
        {"a": {"b": 1}}, ("nope.deep", "a.missing", "a.b.c", "list.9", "")
    )
    assert out == {"a": {"b": 1}}


def test_empty_declaration_still_returns_a_copy() -> None:
    payload = {"a": [1, 2]}
    out = redact_arguments(payload, ())
    assert out == payload and out is not payload


def test_non_string_values_keep_type_hint() -> None:
    out = redact_arguments({"n": 12, "obj": {"k": 1}, "none": None},
                           ("n", "obj", "none"))
    assert out["n"] == "[redacted:int]"
    assert out["obj"] == "[redacted:dict]"
    assert out["none"] == "[redacted]"


# ---------- 执行链集成 ----------

class _Channel:
    def __init__(self) -> None:
        self.seen: dict = {}

    async def emit(self, event) -> None:  # pragma: no cover
        return None

    async def ask(self, question: str) -> str:  # pragma: no cover
        return ""

    async def confirm(self, tool_name: str, arguments: dict) -> bool:
        self.seen = arguments
        return True


def _run_api_call(tool_governance: ToolGovernance):
    calls: list[dict] = []

    def push_order(api_key: str, order_id: str) -> dict:
        """用密钥推送订单。"""
        calls.append({"api_key": api_key, "order_id": order_id})
        return {"ok": True}

    spec = build_spec(push_order)
    spec.governance = tool_governance
    registry = ToolRegistry()
    registry.register(spec)
    channel = _Channel()
    executor = ToolExecutor(
        registry, AllowlistPolicy((), mode="auto"), channel
    )
    events, ok, text = asyncio.run(
        executor.execute("push_order", {"api_key": SECRET, "order_id": "A-1"})
    )
    return events, ok, calls, channel


def test_declared_secret_is_hidden_from_approval_and_audit() -> None:
    events, ok, calls, channel = _run_api_call(
        ToolGovernance(sensitive=("api_key",))
    )
    assert ok is True
    asked = next(e for e in events if e.type == EventType.PERMISSION_ASKED)
    called = next(e for e in events if e.type == EventType.TOOL_CALL)
    assert SECRET not in str(asked.data) and SECRET not in str(called.data)
    assert channel.seen["api_key"].startswith("[redacted")
    # 但执行必须拿真值——脱敏不得改变业务行为
    assert calls == [{"api_key": SECRET, "order_id": "A-1"}]


def test_undeclared_params_stay_visible_by_design() -> None:
    # 边界声明：内核不按字段名猜密钥。没声明就照常展示，
    # "像不像密钥"由宿主负责声明，避免误伤审批可读性。
    events, ok, _calls, channel = _run_api_call(ToolGovernance())
    assert ok is True
    assert channel.seen["api_key"] == SECRET
    assert SECRET in str(next(e for e in events
                              if e.type == EventType.TOOL_CALL).data)
