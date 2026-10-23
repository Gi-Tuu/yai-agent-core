"""桌面审批卡的展示回归：标志不能丢，嵌套参数不能变成 [object Object]。

不加载完整 QML/Window，只把 Panel.qml 的 pendingSummary 绑定抽出来用 QJSEngine
执行；未装 PySide6 时整体跳过。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("PySide6.QtCore", reason="需要 PySide6（uv sync --extra desktop）")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtQml import QJSEngine  # noqa: E402


def test_desktop_channel_keeps_approval_required_flag() -> None:
    from shell.desktop.channel import QtChannel

    class Runtime:
        def __init__(self) -> None:
            self.payload = None

        def open_prompt(self, kind, future, data) -> None:
            self.payload = data
            future.set_result(False)

        def close_prompt(self) -> None:
            return None

    runtime = Runtime()
    channel = QtChannel(runtime)
    payload = {"tool": "transfer", "arguments": {"payment": {"amount": 5000}}}
    approved = asyncio.run(
        channel.confirm(
            "transfer",
            payload["arguments"],
            {"approval_required": True, "reason": "金额超过阈值", "risk": "medium"},
        )
    )
    assert approved is False
    assert runtime.payload["approval_required"] is True
    assert runtime.payload["reason"] == "金额超过阈值"


def test_pending_title_does_not_call_a_high_risk_read_a_write() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    engine = QJSEngine()
    source = (ROOT / "shell" / "desktop" / "qml" / "Panel.qml").read_text(
        encoding="utf-8"
    )
    block = source.split("readonly property string pendingTitle: {", 1)[1].split(
        "\n    }\n", 1
    )[0]
    program = (
        "var workbench={activePendingKind:'confirm'};"
        "var panel={pending:{risk:'high'}};"
        "(function(){" + block + "})()"
    )
    value = engine.evaluate(program)
    assert not value.isError(), value.toString()
    assert value.toString() == "高影响操作待确认"
    del engine
    del app


def test_pending_summary_renders_nested_arguments_readably() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    engine = QJSEngine()
    source = (ROOT / "shell" / "desktop" / "qml" / "Panel.qml").read_text(
        encoding="utf-8"
    )
    block = source.split("readonly property string pendingSummary: {", 1)[1].split(
        "\n    }\n", 1
    )[0]
    payload = {
        "tool": "transfer",
        "arguments": {"payment": {"amount": 5000, "account": "audit"}},
        "reason": "金额超过阈值",
        "risk": "medium",
    }
    program = (
        "var workbench={activePendingKind:'confirm'};"
        f"var panel={{pending:{json.dumps(payload)}}};"
        "(function(){" + block + "})()"
    )
    value = engine.evaluate(program)
    assert not value.isError(), value.toString()
    text = value.toString()
    assert "[object Object]" not in text
    assert '"amount":5000' in text and '"account":"audit"' in text
    assert "金额超过阈值" in text
    del engine
    del app
