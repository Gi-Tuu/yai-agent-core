"""声明式参数脱敏回归：对外副本看不到密钥，执行仍拿到真值（全程离线）。"""

from __future__ import annotations

import asyncio

from yai_core import build_spec
from yai_core.channels import CollectChannel
from yai_core.policy import AllowlistPolicy
from yai_core.policy.redact import redact_arguments
from yai_core.tools import ToolExecutor, ToolRegistry, build_composite_spec
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


def test_broad_mode_replaces_every_top_level_value() -> None:
    """宽口径审计提示（外部契约的 audit.sensitive）：顶层值一律收口，不猜嵌套路径。"""
    out = redact_arguments(
        {"customer": "张三", "amount": 500, "payload": {"deep": SECRET}}, (), broad=True
    )
    assert out == {
        "customer": f"[redacted:len={len('张三')}]",
        "amount": "[redacted:int]",
        "payload": "[redacted:dict]",
    }
    assert SECRET not in str(out)


def test_broad_mode_applies_after_declared_paths() -> None:
    payload = {"a": SECRET, "b": 1}
    out = redact_arguments(payload, ("b",), broad=True)
    assert out["a"].startswith("[redacted") and out["b"].startswith("[redacted")
    assert payload["a"] == SECRET


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


def test_broad_audit_hint_redacts_logs_not_the_approver() -> None:
    """audit.sensitive 的义务范围是 logs：审计事件整体收口，审批副本仍按声明路径脱敏。"""
    events, ok, calls, channel = _run_api_call(ToolGovernance(audit_sensitive=True))
    assert ok is True
    called = next(e for e in events if e.type == EventType.TOOL_CALL)
    asked = next(e for e in events if e.type == EventType.PERMISSION_ASKED)
    assert SECRET not in str(called.data)
    assert "[redacted" in str(called.data)
    # 审批人那份没声明路径 → 保持可读，否则人工批准失去判断依据
    assert asked.data["arguments"]["order_id"] == "A-1"
    assert channel.seen["order_id"] == "A-1"
    assert calls == [{"api_key": SECRET, "order_id": "A-1"}]


def test_broad_hint_still_hides_declared_paths_from_approver() -> None:
    events, _ok, _calls, channel = _run_api_call(
        ToolGovernance(sensitive=("api_key",), audit_sensitive=True)
    )
    asked = next(e for e in events if e.type == EventType.PERMISSION_ASKED)
    assert asked.data["arguments"]["api_key"].startswith("[redacted")
    assert channel.seen["api_key"].startswith("[redacted")
    assert asked.data["arguments"]["order_id"] == "A-1"


# ---------- 组合工具：自己那份对外副本同样受声明约束 ----------

def _run_composite(step_governance: ToolGovernance | None = None) -> tuple[list, object]:
    """组合把入参 ``api_key`` 经占位符透传给内部 ``echo``，返回事件与执行痕迹。"""
    seen: list[dict] = []

    def echo(token: str) -> dict:
        """回显收到的值。"""
        seen.append({"token": token})
        return {"token": token}

    inner = build_spec(echo)
    if step_governance is not None:
        inner.governance = step_governance

    composite = build_composite_spec(
        "leak_chain",
        "把密钥交给内部工具回显",
        [{"tool": "echo", "args": {"token": "{{$input.api_key}}"}}],
        input_schema={"type": "object", "properties": {"api_key": {"type": "string"}}},
    )
    composite.governance = ToolGovernance(sensitive=("api_key",))

    registry = ToolRegistry()
    registry.register(inner)
    registry.register(composite)
    executor = ToolExecutor(
        registry, AllowlistPolicy((), mode="auto"), CollectChannel()
    )
    events, ok, _text = asyncio.run(
        executor.execute("leak_chain", {"api_key": SECRET})
    )
    return events, (ok, seen)


def test_composite_declared_path_redacts_its_own_event() -> None:
    """声明了 sensitive 就不能因为走组合分支而失效：TOOL_COMPOSED 副本要糊掉。"""
    events, (ok, seen) = _run_composite()
    assert ok is True
    composed = next(e for e in events if e.type == EventType.TOOL_COMPOSED)
    assert SECRET not in str(composed.data)
    assert composed.data["arguments"]["api_key"].startswith("[redacted")
    # 执行链仍拿真值——脱敏只改变"别人看见什么"
    assert seen == [{"token": SECRET}]


def test_redaction_is_per_layer_not_taint_tracking() -> None:
    """边界（如实记录，不假装保护）：密钥流进内部步骤后，按**那一步自己的**声明处理。

    组合工具的声明只覆盖它自己那份 arguments；步骤参数名已经换了（``token``），
    内核不做自动污点追踪。宿主想让这一步也收口，就给这一步也声明。
    """
    events, (ok, _seen) = _run_composite()
    assert ok is True
    step_call = next(e for e in events if e.type == EventType.TOOL_CALL)
    assert SECRET in str(step_call.data)

    covered, (ok2, _s2) = _run_composite(ToolGovernance(sensitive=("token",)))
    assert ok2 is True
    step_call2 = next(e for e in covered if e.type == EventType.TOOL_CALL)
    assert SECRET not in str(step_call2.data)
    assert step_call2.data["arguments"]["token"].startswith("[redacted")
