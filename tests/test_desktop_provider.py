"""Windows Desktop Provider 离线测试（v0.26 Batch 5）。

全部用假后端，不启动真实窗口。覆盖：
1. 窗口枚举 / 进程与标题白名单；
2. 观察生成带版本的控件引用（d<N>:<i>），旧引用过期不调用后端；
3. invoke / set_value 的 executed + outcome 语义；
4. 高风险控件默认拒绝、宿主显式放行；
5. 坐标点击默认关闭 + 焦点保护；
6. 用户接管 pause 后所有动作拒绝；
7. 敏感控件拒绝自动填写；
8. 经 ToolRegistry + ToolExecutor 走统一治理并产生收据。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from yai_core.channels import CollectChannel
from yai_core.integrations.desktop import (
    ControlInfo,
    DesktopObservation,
    DesktopPolicy,
    DesktopProvider,
    WindowInfo,
    build_desktop_tools,
)
from yai_core.policy import AllowlistPolicy
from yai_core.tools import ToolExecutor, ToolRegistry


class FakeBackend:
    def __init__(
        self,
        *,
        window_id: str = "1001",
        title: str = "测试窗口",
        process_id: str = "4242",
        controls: tuple[ControlInfo, ...] | None = None,
    ) -> None:
        self.window = WindowInfo(
            window_id=window_id, title=title, process_id=process_id, class_name="TestWindow"
        )
        self.controls = controls or (
            ControlInfo(control_id="1.1", role="Button", name="确定"),
            ControlInfo(control_id="1.2", role="Edit", name="姓名", automation_id="name"),
            ControlInfo(
                control_id="1.3",
                role="Edit",
                name="密码",
                automation_id="password",
                sensitive=True,
            ),
            ControlInfo(control_id="1.4", role="Button", name="删除全部记录"),
            ControlInfo(
                control_id="1.5",
                role="ComboBox",
                name="字体",
                automation_id="font",
                value="Arial",
            ),
        )
        self.fingerprint = "fp1"
        self.invoked: list[str] = []
        self.set_values: list[tuple[str, str]] = []
        self.clicked: list[str] = []
        self.focused: list[str] = []
        self.selected: list[tuple[str, str]] = []
        self.foreground = True
        self.observe_fail = False
        self.change_on_action = True

    def observation(self) -> DesktopObservation:
        return DesktopObservation(
            window=self.window,
            controls=self.controls,
            focused="1.1",
            fingerprint=self.fingerprint,
        )

    async def list_windows(self) -> list[WindowInfo]:
        return [self.window]

    async def observe(self, window_id: str) -> DesktopObservation:
        if self.observe_fail:
            raise RuntimeError("boom")
        return self.observation()

    async def focus(self, window_id: str) -> None:
        self.focused.append(window_id)

    async def is_foreground(self, window_id: str) -> bool:
        return self.foreground

    async def invoke(self, window_id: str, control_id: str) -> None:
        self.invoked.append(control_id)
        if self.change_on_action:
            self.fingerprint = "fp2"

    async def set_value(self, window_id: str, control_id: str, value: str) -> None:
        self.set_values.append((control_id, value))
        if self.change_on_action:
            self.fingerprint = "fp2"

    async def select_option(self, window_id: str, control_id: str, value: str) -> None:
        self.selected.append((control_id, value))
        if self.change_on_action:
            self.fingerprint = "fp2"

    async def click(self, window_id: str, control_id: str) -> None:
        self.clicked.append(control_id)

    async def screenshot(self, window_id: str) -> bytes | None:
        return None


def _run(coro):
    return asyncio.run(coro)


def _provider(backend: FakeBackend, **policy_kw: Any) -> DesktopProvider:
    return DesktopProvider(backend, policy=DesktopPolicy(**policy_kw))


# ---------- 观察 / 白名单 ----------


def test_list_windows_hides_disallowed_windows() -> None:
    backend = FakeBackend()
    provider = _provider(backend, allowed_processes=frozenset({"9999"}))
    result = _run(provider.list_windows())
    assert result["windows"] == []
    assert result["hidden_count"] == 1

    allowed = _provider(backend, allowed_processes=frozenset({"4242"}))
    result = _run(allowed.list_windows())
    assert result["windows"][0]["allowed"] is True
    assert result["hidden_count"] == 0


def test_observe_builds_versioned_refs() -> None:
    provider = _provider(FakeBackend())
    result = _run(provider.observe("1001"))

    assert result["status"] == "observed"
    assert result["snapshot_id"] == "d1"
    assert [item["ref"] for item in result["controls"]] == [
        "d1:0",
        "d1:1",
        "d1:2",
        "d1:3",
        "d1:4",
    ]
    assert result["evidence"][0]["kind"] == "desktop"


def test_observe_disallowed_window_returns_metadata_only() -> None:
    provider = _provider(FakeBackend(), allowed_processes=frozenset({"9999"}))
    result = _run(provider.observe("1001"))

    assert result["status"] == "window_not_allowed"
    assert "controls" not in result


def test_stale_ref_rejected_without_backend_call() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    _run(provider.observe("1001"))
    _run(provider.observe("1001"))

    result = _run(provider.invoke("d1:0"))

    assert result["status"] == "stale_ref"
    assert backend.invoked == []


# ---------- 动作 ----------


def test_invoke_reports_executed_and_change() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    _run(provider.observe("1001"))

    result = _run(provider.invoke("d1:0"))

    assert backend.invoked == ["1.1"]
    assert result["status"] == "executed"
    assert result["outcome"] == "changed"
    assert result["snapshot_id"] == "d2"
    assert "success" not in result


def test_set_value_uses_value_pattern() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    _run(provider.observe("1001"))

    result = _run(provider.set_value("d1:1", "张三"))

    assert backend.set_values == [("1.2", "张三")]
    assert result["status"] == "executed"


def test_sensitive_control_blocked() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    _run(provider.observe("1001"))

    result = _run(provider.set_value("d1:2", "secret"))

    assert result["status"] == "sensitive_control_blocked"
    assert backend.set_values == []


def test_high_risk_control_blocked_by_default() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    _run(provider.observe("1001"))

    result = _run(provider.invoke("d1:3"))

    assert result["status"] == "high_risk_blocked"
    assert backend.invoked == []


def test_high_risk_control_allowed_when_host_opts_in() -> None:
    backend = FakeBackend()
    provider = _provider(backend, allow_high_risk_controls=True)
    _run(provider.observe("1001"))

    result = _run(provider.invoke("d1:3"))

    assert result["status"] == "executed"
    assert backend.invoked == ["1.4"]


def test_geometry_click_disabled_by_default() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    _run(provider.observe("1001"))

    result = _run(provider.click("d1:0"))

    assert result["status"] == "geometry_click_disabled"
    assert backend.clicked == []


def test_geometry_click_requires_foreground() -> None:
    backend = FakeBackend()
    backend.foreground = False
    provider = _provider(backend, allow_geometry_click=True)
    _run(provider.observe("1001"))

    result = _run(provider.click("d1:0"))

    assert result["status"] == "not_foreground"
    assert backend.clicked == []

    backend.foreground = True
    assert _run(provider.click("d1:0"))["status"] == "executed"
    assert backend.clicked == ["1.1"]


def test_focus_and_window_not_found() -> None:
    backend = FakeBackend()
    provider = _provider(backend)

    assert _run(provider.focus("1001"))["status"] == "executed"
    assert backend.focused == ["1001"]
    assert _run(provider.focus("9999"))["status"] == "window_not_found"


# ---------- 用户接管 ----------


def test_pause_blocks_actions_but_not_reads() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    _run(provider.observe("1001"))
    provider.pause()

    # 只读观察在接管期间仍可用。
    assert _run(provider.observe("1001"))["status"] == "observed"
    assert _run(provider.list_windows())["status"] == "observed"
    # 会造成输入 / 状态变化的动作全部拒绝。
    assert _run(provider.invoke("d2:0"))["status"] == "paused"
    assert _run(provider.set_value("d2:1", "x"))["status"] == "paused"
    assert _run(provider.click("d2:0"))["status"] in {"paused", "geometry_click_disabled"}
    assert backend.invoked == [] and backend.set_values == []

    provider.resume()
    assert _run(provider.invoke("d2:0"))["status"] == "executed"


# ---------- 统一治理 ----------


def _executor(provider: DesktopProvider) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_many(build_desktop_tools(provider))
    return ToolExecutor(registry, AllowlistPolicy((), mode="auto"), CollectChannel())


def test_tools_go_through_executor_with_receipt() -> None:
    provider = _provider(FakeBackend(), require_approval_for=frozenset())
    executor = _executor(provider)

    events, ok, text = _run(executor.execute("desktop_list_windows", {}))

    assert ok is True
    assert "windows" in text
    receipts = [event for event in events if event.type == "tool_result"]
    assert receipts and "receipt" in receipts[-1].data


def test_invoke_tool_declares_approval_by_default() -> None:
    provider = _provider(FakeBackend())
    tools = {spec.name: spec for spec in build_desktop_tools(provider)}
    approval = tools["desktop_invoke"].governance.approval
    assert approval is not None and approval.required is True
    assert tools["desktop_observe"].governance.approval is None


def test_set_value_declares_sensitive_value() -> None:
    provider = _provider(FakeBackend())
    tools = {spec.name: spec for spec in build_desktop_tools(provider)}
    assert tools["desktop_set_value"].governance.sensitive == ("value",)
    assert tools["desktop_select"].governance.sensitive == ("value",)


def test_select_option_uses_real_backend_and_declares_approval() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    observed = _run(provider.observe("1001"))
    combo = next(
        c for c in observed["controls"] if c.get("role") == "ComboBox"
    )
    result = _run(provider.select_option(combo["ref"], "Arial"))
    assert result["ok"] is True
    assert backend.selected == [("1.5", "Arial")]
    tools = {spec.name: spec for spec in build_desktop_tools(provider)}
    assert tools["desktop_select"].governance.approval is not None


@pytest.mark.parametrize("bad", [{"max_controls": 0}, {"action_timeout_ms": -1}])
def test_invalid_policy_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        DesktopPolicy(**bad)
