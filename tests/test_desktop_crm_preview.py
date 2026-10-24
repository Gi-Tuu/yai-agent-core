"""桌面销售预览闭包：把 host_e 端点结果原样交给审批卡。"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="需要 PySide6（uv sync --extra desktop）")

from shell.desktop.crm_client import (  # noqa: E402
    HostECrmClient,
    build_crm_preview,
    build_crm_state,
)


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _preview(self, tool_name: str, arguments: dict) -> dict:
        self.calls.append((tool_name, arguments))
        return {
            "summary": "将新增跟进",
            "token": "rev-1",
            "diff": "+ 回访",
            "target": "customer/王敏",
        }


def test_preview_helper_is_not_a_discoverable_tool() -> None:
    """`preview` 是审批内部能力，不能作为公开方法被 AgentCore.auto 注册成工具。"""
    assert not hasattr(HostECrmClient, "preview")
    assert hasattr(HostECrmClient, "_preview")


def test_selection_state_helper_is_private_and_returns_snapshot() -> None:
    class _StateClient:
        def _selection(self) -> dict:
            return {"selected_customer": "王敏", "company": "华东智造集团"}

    assert not hasattr(HostECrmClient, "selection")
    assert hasattr(HostECrmClient, "_selection")
    assert build_crm_state(_StateClient())() == {
        "selected_customer": "王敏",
        "company": "华东智造集团",
    }


def test_crm_preview_closure_passes_real_endpoint_payload() -> None:
    client = _Client()
    preview = build_crm_preview(client, "add_followup")
    result = preview({"customer": "王敏", "content": "回访"}, None)
    assert client.calls == [
        ("add_followup", {"customer": "王敏", "content": "回访"})
    ]
    assert result["summary"] == "将新增跟进"
    assert result["token"] == "rev-1"
