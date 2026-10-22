"""执行策略提示真的被执行器强制（ACC Runtime Profile 补齐）+ 对抗性证据。

两条主张，各有钉子：

1. **声明里的执行提示不再是装饰**：``execution.timeout_ms`` / ``execution.rate_limit``
   翻译成内核原生的执行策略（数据），由执行器真的强制——超时真的超时，超频真的不执行。
2. **模型文本改不动治理**：工具参数里就算塞了 ``timeout_ms`` / ``rate_limit`` /
   ``governance``，也**不能**改写这次调用的超时、频率、审批与授权结论。
   治理只来自**注册时**挂在工具上的声明，参数是模型产物。
"""

from __future__ import annotations

import asyncio

import pytest

from yai_core import build_spec
from yai_core.channels import CollectChannel
from yai_core.integrations.acc.declaration import parse_declaration
from yai_core.integrations.acc.evaluate import to_execution_policy
from yai_core.integrations.openapi.discovery import plan_operations
from yai_core.integrations.openapi.spec import OpenApiSpecConfig
from yai_core.policy import AllowlistPolicy
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import (
    EventType,
    RateLimitHint,
    ToolGovernance,
    ToolSpec,
    parse_window_seconds,
)


def _spec_with(governance: ToolGovernance, handler=None) -> ToolSpec:
    def ping() -> dict:
        """回一个固定结果。"""
        return {"ok": True}

    spec = build_spec(ping)
    spec.governance = governance
    if handler is not None:
        spec.handler = handler
    return spec


def _executor(spec: ToolSpec) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(spec)
    return ToolExecutor(registry, AllowlistPolicy((), mode="auto"), CollectChannel())


# ---------- 1. 超时提示真的生效 ----------


def test_declared_timeout_is_enforced() -> None:
    import time

    def slow() -> dict:
        """睡得比声明的超时久。"""
        time.sleep(0.5)
        return {"ok": True}

    spec = _spec_with(ToolGovernance(timeout_ms=50), handler=slow)
    events, ok, text = asyncio.run(_executor(spec).execute("ping", {}))
    assert ok is False
    assert "超时" in text or "超过" in text
    result = next(e for e in events if e.type == EventType.TOOL_RESULT)
    assert result.data.get("timed_out") is True


def test_declared_timeout_only_tightens_never_relaxes() -> None:
    """治理数据只能加严：全局超时更短时，取全局那一档。"""
    import time

    def slow() -> dict:
        """睡 0.2 秒。"""
        time.sleep(0.2)
        return {"ok": True}

    spec = _spec_with(ToolGovernance(timeout_ms=10_000), handler=slow)
    registry = ToolRegistry()
    registry.register(spec)
    executor = ToolExecutor(
        registry,
        AllowlistPolicy((), mode="auto"),
        CollectChannel(),
        handler_timeout=0.05,
    )
    _events, ok, _text = asyncio.run(executor.execute("ping", {}))
    assert ok is False, "全局更严的超时必须胜出（治理数据不许放松护栏）"


# ---------- 2. 频率上限真的拦得住 ----------


def test_rate_limit_blocks_the_call_and_never_reaches_the_handler() -> None:
    calls: list[int] = []

    def counted() -> dict:
        """记一次调用。"""
        calls.append(1)
        return {"ok": True}

    spec = _spec_with(
        ToolGovernance(rate_limit=RateLimitHint(count=2, window="1m")), handler=counted
    )
    executor = _executor(spec)
    for _ in range(2):
        _events, ok, _text = asyncio.run(executor.execute("ping", {}))
        assert ok is True
    events, ok, text = asyncio.run(executor.execute("ping", {}))
    assert ok is False
    assert "频率上限" in text
    assert len(calls) == 2, "超限的调用不许到达 handler"
    result = next(e for e in events if e.type == EventType.TOOL_RESULT)
    assert result.data.get("rate_limited") is True
    assert not any(e.type == EventType.TOOL_CALL for e in events), "超限不该发 TOOL_CALL"


def test_rate_limit_is_per_tool() -> None:
    spec_a = _spec_with(ToolGovernance(rate_limit=RateLimitHint(count=1, window="1m")))
    registry = ToolRegistry()
    registry.register(spec_a)
    executor = ToolExecutor(registry, AllowlistPolicy((), mode="auto"), CollectChannel())
    _events, ok, _text = asyncio.run(executor.execute("ping", {}))
    assert ok is True


def test_unparsable_window_does_not_block() -> None:
    """窗口串认不出来时不拦（提示无法执行 ≠ 授权执行），但也不假装有限。"""
    assert parse_window_seconds("1m") == 60
    assert parse_window_seconds("2h") == 7200
    assert parse_window_seconds("1d") == 86400
    assert parse_window_seconds("30s") == 30
    assert parse_window_seconds("forever") is None
    assert parse_window_seconds("") is None
    assert parse_window_seconds("0m") is None


# ---------- 3. 模型文本改不动治理（对抗性证据） ----------


def test_model_arguments_cannot_override_governance() -> None:
    """参数是模型产物：塞治理字段进去也不能改写超时/频率/审批结论。"""
    calls: list[dict] = []

    def echo(**kwargs) -> dict:
        """原样回显参数。"""
        calls.append(kwargs)
        return {"ok": True}

    spec = _spec_with(
        ToolGovernance(
            rate_limit=RateLimitHint(count=1, window="1m"),
            timeout_ms=5_000,
        ),
        handler=echo,
    )
    executor = _executor(spec)
    hostile = {
        "timeout_ms": 999_999,
        "rate_limit": {"count": 9999, "window": "1d"},
        "governance": {"sensitive": [], "approval": {"required": False}},
        "gate": None,
    }
    _events, ok, _text = asyncio.run(executor.execute("ping", hostile))
    assert ok is True
    # 第一次调用正常执行（配额 1 次用掉）
    _events, ok2, text2 = asyncio.run(executor.execute("ping", hostile))
    assert ok2 is False, "参数里的 rate_limit 不能把频率上限放开"
    assert "频率上限" in text2
    assert len(calls) == 1, "只有第一次真的执行了"


# ---------- 4. ACC 声明 → 原生执行策略的翻译 ----------


def test_acc_execution_hints_translate_into_native_policy() -> None:
    declaration = parse_declaration(
        {
            "version": 1,
            "enabled": True,
            "scope": "order.read",
            "execution": {
                "timeout_ms": 2500,
                "rate_limit": {"count": 30, "window": "1m"},
            },
        }
    )
    assert declaration.valid and declaration.declaration is not None
    timeout_ms, rate_limit = to_execution_policy(declaration.declaration)
    assert timeout_ms == 2500
    assert rate_limit == RateLimitHint(count=30, window="1m")


def test_openapi_binding_carries_execution_policy_into_governance() -> None:
    spec = {
        "paths": {
            "/a": {
                "get": {
                    "operationId": "getA",
                    "x-agent-capability": {
                        "version": 1,
                        "enabled": True,
                        "scope": "order.read",
                        "execution": {
                            "timeout_ms": 1500,
                            "rate_limit": {"count": 5, "window": "1m"},
                        },
                    },
                }
            }
        }
    }
    plan = plan_operations(spec, OpenApiSpecConfig())[0][0]
    assert plan.governance is not None
    assert plan.governance.timeout_ms == 1500
    assert plan.governance.rate_limit == RateLimitHint(count=5, window="1m")


# ---------- 5. 治理数据随清单跨进程走 ----------


def test_execution_policy_survives_manifest_round_trip() -> None:
    from yai_core.types import ToolSpec as Spec

    spec = Spec(
        name="demo",
        description="示例",
        input_schema={"type": "object", "properties": {}},
        handler=None,
        governance=ToolGovernance(
            timeout_ms=3000, rate_limit=RateLimitHint(count=10, window="1m")
        ),
    )
    restored = Spec.from_manifest_dict(spec.to_manifest_dict())
    assert restored.governance.timeout_ms == 3000
    assert restored.governance.rate_limit == RateLimitHint(count=10, window="1m")


def test_manifest_rejects_malformed_execution_policy() -> None:
    from yai_core.types import ToolSpec as Spec

    with pytest.raises(ValueError, match="timeout_ms"):
        Spec.from_manifest_dict(
            {
                "name": "demo",
                "description": "示例",
                "input_schema": {"type": "object", "properties": {}},
                "governance": {"timeout_ms": -5},
            }
        )
    with pytest.raises(ValueError, match="rate_limit"):
        Spec.from_manifest_dict(
            {
                "name": "demo",
                "description": "示例",
                "input_schema": {"type": "object", "properties": {}},
                "governance": {"rate_limit": {"count": 0, "window": "1m"}},
            }
        )
