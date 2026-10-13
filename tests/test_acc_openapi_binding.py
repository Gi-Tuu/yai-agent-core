"""ACC 的 OpenAPI 绑定测试：operation 级 `x-agent-capability` → 暴露 / 跳过 / 治理快照。

离线原则：spec 全部内联构造，唯一的网络走 httpx.MockTransport。
核心断言方向只有一个——**声明不可用时宁可少一个工具，也绝不给出一个没有治理的同名工具**。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from yai_core.integrations.acc.openapi import bind_operation, misplaced_declarations
from yai_core.integrations.openapi.discovery import plan_operations
from yai_core.integrations.openapi.spec import OpenApiSpecConfig

ORDER_READ: dict[str, Any] = {"version": 1, "enabled": True, "scope": "order.read"}


def _spec(operations: dict[str, dict]) -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {f"/{i}": {"get": op} for i, op in operations.items()},
    }


def _bind(
    *,
    declaration: dict | None = ORDER_READ,
    **kwargs: Any,
) -> Any:
    op: dict[str, Any] = {"operationId": "op"}
    if declaration is not None:
        op["x-agent-capability"] = declaration
    defaults: dict[str, Any] = {
        "method": "POST",
        "tool_name": "op",
        "input_schema": {
            "type": "object",
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
        },
    }
    defaults.update(kwargs)
    return bind_operation(op, **defaults)


# --- 零影响：没有扩展的操作照旧 ------------------------------------------------


def test_operation_without_extension_is_untouched() -> None:
    binding = _bind(declaration=None)
    assert binding.declared is False
    assert binding.exposed is True
    assert binding.raw == {}
    plans, notes = plan_operations(_spec({"a": {"operationId": "getA"}}), OpenApiSpecConfig())
    assert [p.name for p in plans] == ["get_a"]
    assert plans[0].governance is None
    assert notes == []


# --- 暴露路径：治理快照与 guidance --------------------------------------------


def test_valid_declaration_lands_on_plan_governance() -> None:
    binding = _bind(declaration={**ORDER_READ, "execution": {"readonly": True}})
    assert binding.exposed is True
    assert binding.scope == "order.read"
    assert binding.effective_risk == "low"
    assert binding.raw["scope"] == "order.read"

    spec = _spec(
        {
            "a": {
                "operationId": "getA",
                "x-agent-capability": {
                    **ORDER_READ,
                    "guidance": {"when_to_use": "用户问订单状态时", "returns": "订单状态"},
                },
            }
        }
    )
    plan = plan_operations(spec, OpenApiSpecConfig())[0][0]
    assert plan.governance is not None
    assert plan.governance.audit_sensitive is False
    assert plan.governance.acc["guidance"]["when_to_use"] == "用户问订单状态时"
    assert "用户问订单状态时" in plan.description
    assert "返回：订单状态" in plan.description


def test_audit_sensitive_hint_maps_to_governance() -> None:
    binding = _bind(declaration={**ORDER_READ, "audit": {"sensitive": True}})
    assert binding.audit_sensitive is True
    spec = _spec(
        {
            "a": {
                "operationId": "getA",
                "parameters": [{"name": "id", "in": "path", "schema": {"type": "string"}}],
                "x-agent-capability": {**ORDER_READ, "audit": {"sensitive": True}},
            }
        }
    )
    plan = plan_operations(spec, OpenApiSpecConfig())[0][0]
    assert plan.governance is not None and plan.governance.audit_sensitive is True


# --- 不暴露：格式错 / 主版本 / enabled=false ----------------------------------


@pytest.mark.parametrize(
    "declaration,fragment",
    [
        ({"version": 2, "enabled": True, "scope": "order.read"}, "unsupported_version"),
        ({"version": 1, "enabled": True}, "invalid_declaration"),
        ({"version": 1, "enabled": True, "scope": "bad scope!"}, "invalid_declaration"),
        ({"version": 1, "enabled": "true", "scope": "order.read"}, "invalid_declaration"),
    ],
)
def test_broken_declaration_skips_the_operation(declaration: dict, fragment: str) -> None:
    binding = _bind(declaration=declaration)
    assert binding.declared is True
    assert binding.exposed is False
    assert fragment in binding.diagnostics[0]
    # 关键：没有治理的同名工具绝不能出现。
    spec = _spec({"a": {"operationId": "getA", "x-agent-capability": declaration}})
    plans, notes = plan_operations(spec, OpenApiSpecConfig())
    assert plans == []
    assert any("不回退成无治理工具" in note for note in notes)


def test_non_object_extension_is_skipped() -> None:
    binding = _bind(declaration=None)  # 先拿一个干净 binding 确认默认路径
    assert binding.declared is False
    op = {"operationId": "op", "x-agent-capability": "order.read"}
    binding = bind_operation(op, method="GET", tool_name="op", input_schema={})
    assert binding.exposed is False and "不是 object" in binding.diagnostics[0]


def test_disabled_capability_is_not_exposed() -> None:
    binding = _bind(declaration={"version": 1, "enabled": False, "scope": "order.read"})
    assert binding.exposed is False
    assert "enabled=false" in binding.diagnostics[0]


# --- 主体与路由 ---------------------------------------------------------------


def test_subject_required_needs_trusted_subject() -> None:
    declaration = {**ORDER_READ, "subject": {"required": True}}
    hidden = _bind(declaration=declaration)
    assert hidden.exposed is False and "可信主体" in hidden.diagnostics[0]
    shown = _bind(declaration=declaration, trusted_subject=True)
    assert shown.exposed is True


def test_scope_allowlist_filters_exposure() -> None:
    hidden = _bind(declaration=ORDER_READ, scope_policy=lambda scope: False)
    assert hidden.exposed is False and "路由" in hidden.diagnostics[0]
    assert _bind(declaration=ORDER_READ, scope_policy=lambda s: s == "order.read").exposed is True


def test_config_prefix_matching_for_acc_scopes() -> None:
    config = OpenApiSpecConfig(acc_scopes=("order.*", "refund.create"))
    assert config.acc_scope_allowed("order.read") is True
    assert config.acc_scope_allowed("refund.create") is True
    assert config.acc_scope_allowed("refund.delete") is False
    assert config.acc_scope_allowed("order") is False
    assert OpenApiSpecConfig().acc_scope_allowed("anything") is True


# --- approval.when 与绑定输入 schema ----------------------------------------


def test_condition_param_must_resolve_in_binding_view() -> None:
    declaration = {
        **ORDER_READ,
        "approval": {"when": [{"param": "not.declared", "op": "exists"}]},
    }
    binding = _bind(declaration=declaration)
    assert binding.exposed is False
    assert "不在绑定输入 schema 里" in binding.diagnostics[0]


def test_nested_condition_param_resolves_through_schema() -> None:
    declaration = {
        **ORDER_READ,
        "approval": {"when": [{"param": "billing.tier", "op": "==", "value": "gold"}]},
    }
    schema = {
        "type": "object",
        "properties": {
            "billing": {"type": "object", "properties": {"tier": {"type": "string"}}}
        },
    }
    assert _bind(declaration=declaration, input_schema=schema).exposed is True


def test_ambiguous_normalized_path_skips_operation() -> None:
    """同名参数同时来自 query 与 body：绑定禁止静默二选一，整条不暴露。"""
    declaration = {
        **ORDER_READ,
        "approval": {"when": [{"param": "amount", "op": ">", "value": 10}]},
    }
    binding = _bind(
        declaration=declaration,
        query_params=("amount",),
        body_fields=("amount",),
    )
    assert binding.exposed is False
    assert "歧义" in binding.diagnostics[0]


def test_skipped_operation_does_not_burn_a_tool_name() -> None:
    spec = _spec(
        {
            "a": {
                "operationId": "getA",
                "x-agent-capability": {"version": 2, "enabled": True, "scope": "order.read"},
            },
            "b": {"operationId": "getA"},
        }
    )
    plans, _notes = plan_operations(spec, OpenApiSpecConfig())
    assert [p.name for p in plans] == ["get_a"]


# --- 有效风险默认值（绑定原生信号） ------------------------------------------


def test_get_defaults_low_and_post_defaults_medium() -> None:
    assert _bind(declaration=ORDER_READ, method="GET").effective_risk == "low"
    assert _bind(declaration=ORDER_READ, method="POST").effective_risk == "medium"
    assert (
        _bind(
            declaration={**ORDER_READ, "risk": {"level": "high"}},
            method="GET",
        ).effective_risk
        == "high"
    )


# --- 贴错位置的扩展只出诊断，不生效 ------------------------------------------


def test_misplaced_extensions_are_reported_not_read() -> None:
    spec: dict[str, Any] = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "x-agent-capability": dict(ORDER_READ),
        "paths": {
            "/a": {
                "x-agent-capability": dict(ORDER_READ),
                "get": {"operationId": "getA"},
            }
        },
    }
    notes = misplaced_declarations(spec)
    assert any("根节点" in note for note in notes)
    assert any("/a" in note for note in notes)
    plans, plan_notes = plan_operations(spec, OpenApiSpecConfig())
    # 根/pathItem 上的声明不参与判定：操作照常暴露、但没有任何 ACC 治理。
    assert [p.name for p in plans] == ["get_a"]
    assert plans[0].governance is None
    assert len(plan_notes) == 2


# --- 端到端：治理随 ToolSpec 进注册表 ----------------------------------------


def test_governance_reaches_the_registry() -> None:
    pytest.importorskip("httpx")
    import httpx

    from yai_core.integrations.openapi import OpenApiToolBridge

    spec = _spec(
        {
            "a": {
                "operationId": "getA",
                "parameters": [{"name": "id", "in": "path", "schema": {"type": "string"}}],
                "x-agent-capability": {**ORDER_READ, "audit": {"sensitive": True}},
            }
        }
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    config = OpenApiSpecConfig(spec=spec, base_url_override="http://api.invalid")
    bridge = OpenApiToolBridge(config, transport=transport)
    try:
        specs = asyncio.run(bridge.connect())
    finally:
        asyncio.run(bridge.aclose())
    assert specs[0].governance.audit_sensitive is True
    assert specs[0].governance.acc["scope"] == "order.read"
