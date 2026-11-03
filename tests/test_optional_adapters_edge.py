"""可选适配器的边界分支测试（v0.26）：提高策略 / 模型 / 协议转换的覆盖。

这些用例只测纯数据与策略分支，不启动浏览器 / 桌面 / 真实 UIA。
"""

from __future__ import annotations

import io

import pytest

from yai_core.integrations.browser import (
    BrowserBackendError,
    BrowserPolicy,
    BrowserProvider,
    ElementInfo,
    TabInfo,
    origin_allowed,
    origin_of,
)
from yai_core.integrations.desktop import (
    ControlInfo,
    DesktopPolicy,
    DesktopProvider,
    WindowInfo,
)
from yai_core.sidecar.native_bridge import (
    BridgeConfig,
    BridgeError,
    _normalize_origin,
    encode_frame,
    read_frame,
)

# ---------- Browser 纯函数 / 策略 ----------


def test_origin_of_and_allowlist() -> None:
    assert origin_of("https://Example.com/a?b=1") == "https://example.com"
    assert origin_of("not a url") == ""
    assert origin_allowed("https://a.test/x", frozenset()) is True  # 空 = 不限制
    assert origin_allowed("https://a.test/x", frozenset({"https://a.test"})) is True
    assert origin_allowed("https://b.test/x", frozenset({"https://a.test"})) is False
    assert origin_allowed("https://b.test/x", frozenset({"*"})) is True
    assert origin_allowed(
        "https://sub.a.test/x", frozenset({"https://*.a.test"})
    ) is True
    assert origin_allowed(
        "https://a.test/x", frozenset({"https://*.a.test"})
    ) is False
    assert origin_allowed("ftp://a.test/x", frozenset({"https://a.test"})) is False


def test_browser_policy_approval_and_rate_limit() -> None:
    policy = BrowserPolicy(
        require_approval_for=frozenset({"click"}), rate_limit_per_minute=30
    )
    approval = policy.approval_for("click")
    assert approval is not None and approval.required is True
    assert policy.approval_for("observe") is None
    assert policy.rate_limit() is not None and policy.rate_limit().count == 30
    assert BrowserPolicy(rate_limit_per_minute=None).rate_limit() is None


def test_element_and_tab_public_shapes() -> None:
    element = ElementInfo(
        backend_ref="b0",
        role="textbox",
        name="密码",
        tag="input",
        input_type="password",
        value="secret",
        sensitive=True,
        disabled=True,
        frame_id="f1",
    )
    public = element.to_public("s1:0")
    assert public["sensitive"] is True
    assert "value" not in public
    assert public["frame_id"] == "f1"

    plain = ElementInfo(backend_ref="b1", role="button", name="提交", value="v")
    assert plain.to_public("s1:1")["value"] == "v"

    tab = TabInfo(tab_id="tab-0", url="https://a.test", title="标题", active=True)
    assert tab.to_public()["active"] is True


def test_browser_backend_error_on_observe() -> None:
    class _Boom:
        async def observe(self):
            raise BrowserBackendError("page closed")

    provider = BrowserProvider(_Boom())
    import asyncio

    result = asyncio.run(provider.observe())
    assert result["status"] == "backend_error"

    class _WrongShape:
        async def observe(self):
            return "nope"

    assert asyncio.run(BrowserProvider(_WrongShape()).observe())["status"] == "backend_error"


# ---------- Desktop 纯函数 / 策略 ----------


def test_desktop_policy_filters_and_risk() -> None:
    policy = DesktopPolicy(
        allowed_processes=frozenset({"4242"}),
        allowed_window_titles=frozenset({"测试"}),
        require_approval_for=frozenset({"invoke"}),
        rate_limit_per_minute=5,
    )
    assert policy.allows_window("1", "测试窗口", "4242") is True
    assert policy.allows_window("1", "别的窗口", "4242") is False
    assert policy.allows_window("1", "测试窗口", "9999") is False
    assert policy.is_high_risk_name("删除全部记录") is True
    assert policy.is_high_risk_name("Uninstall") is True
    assert policy.is_high_risk_name("确定") is False
    approval = policy.approval_for("invoke")
    assert approval is not None and approval.required is True
    assert policy.approval_for("observe") is None
    assert policy.rate_limit() is not None
    assert DesktopPolicy(rate_limit_per_minute=None).rate_limit() is None
    assert policy.to_public()["allow_high_risk_controls"] is False


def test_window_and_control_public_shapes() -> None:
    window = WindowInfo(window_id="1", title="标题", process_id="42", class_name="C")
    assert window.to_public()["process_id"] == "42"

    sensitive = ControlInfo(
        control_id="1.1",
        role="Edit",
        name="密码",
        automation_id="pw",
        value="secret",
        sensitive=True,
        enabled=False,
    )
    public = sensitive.to_public("d1:0")
    assert public["sensitive"] is True
    assert "value" not in public
    assert public["enabled"] is False

    plain = ControlInfo(control_id="1.2", role="Button", name="确定", value="")
    assert plain.to_public("d1:1")["role"] == "Button"


def test_desktop_ref_before_observe_is_stale() -> None:
    class _Backend:
        async def observe(self, window_id: str):
            raise AssertionError("不应调用后端")

    import asyncio

    provider = DesktopProvider(_Backend())
    result = asyncio.run(provider.invoke("d0:0"))
    assert result["status"] == "stale_ref"


# ---------- Native Bridge 纯函数 ----------


def test_frame_eof_and_partial_body() -> None:
    assert read_frame(io.BytesIO(b"")) is None
    partial = io.BytesIO((5).to_bytes(4, "little") + b"{}")
    with pytest.raises(BridgeError):
        read_frame(partial)


def test_encode_frame_is_utf8_json() -> None:
    frame = encode_frame({"op": "run", "task": "中文任务"})
    assert "中文".encode() in frame


def test_origin_normalization_rejects_other_schemes() -> None:
    assert _normalize_origin("chrome-extension://abc/") == "chrome-extension://abc"
    assert _normalize_origin("https://abc/") == ""
    assert _normalize_origin("") == ""
    assert BridgeConfig(
        allowed_origins=frozenset({"chrome-extension://abc"})
    ).check_origin("chrome-extension://abc") == "chrome-extension://abc"
