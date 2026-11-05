"""适配器安全默认值：显式 secure preset、特殊协议默认拒绝、宽松模式必须告警。"""

from __future__ import annotations

import warnings

import pytest

from yai_core.integrations.browser import (
    BrowserPolicy,
    BrowserProvider,
    BrowserSecurityWarning,
)
from yai_core.integrations.desktop import (
    DesktopPolicy,
    DesktopProvider,
    DesktopSecurityWarning,
)


class _NoopBackend:
    pass


def test_browser_defaults_reject_special_schemes() -> None:
    policy = BrowserPolicy.development()
    assert policy.allows_origin("https://example.com/page") is True
    for url in (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "about:blank",
    ):
        assert policy.allows_origin(url) is False, url
    assert BrowserPolicy(allow_special_schemes=True).allows_origin(
        "file:///tmp/a.html"
    ) is True


def test_browser_secure_preset_requires_explicit_origins_and_approval() -> None:
    with pytest.raises(ValueError):
        BrowserPolicy.secure([])
    with pytest.raises(ValueError):
        BrowserPolicy.secure(["*"])
    with pytest.raises(ValueError):
        BrowserPolicy.secure(["file:///tmp/a"])

    policy = BrowserPolicy.secure(["https://app.example.com"])
    assert policy.allowed_origins == frozenset({"https://app.example.com"})
    assert policy.require_approval_for >= {
        "click",
        "type",
        "select",
        "open",
        "switch_tab",
    }
    assert policy.allow_password_fields is False
    assert policy.allow_downloads is False
    assert policy.allows_origin("https://app.example.com/x") is True
    assert policy.allows_origin("https://evil.example.com/x") is False


def test_browser_provider_warns_on_permissive_default() -> None:
    with pytest.warns(BrowserSecurityWarning):
        provider = BrowserProvider(_NoopBackend())  # type: ignore[arg-type]
    assert provider.policy.is_permissive is True

    secure = BrowserPolicy.secure(["https://app.example.com"])
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        BrowserProvider(_NoopBackend(), policy=secure)  # type: ignore[arg-type]
    assert not any(isinstance(item.message, BrowserSecurityWarning) for item in captured)


def test_browser_warns_on_explicit_but_permissive_policy() -> None:
    """显式传入但来源不受限也要告警（与桌面侧对齐）。"""
    permissive = [
        BrowserPolicy(),
        BrowserPolicy.development(),
        BrowserPolicy(allow_special_schemes=True),
        # 只配了审批、没配来源白名单：is_permissive=False，但来源仍未受限，照样告警。
        BrowserPolicy(require_approval_for=frozenset({"click"})),
        BrowserPolicy(allowed_origins=frozenset({"*"})),
    ]
    for policy in permissive:
        with pytest.warns(BrowserSecurityWarning, match="来源"):
            BrowserProvider(_NoopBackend(), policy=policy)  # type: ignore[arg-type]


def test_browser_origins_restricted_policy_does_not_warn() -> None:
    policy = BrowserPolicy(allowed_origins=frozenset({"https://app.example.com"}))
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        provider = BrowserProvider(_NoopBackend(), policy=policy)  # type: ignore[arg-type]
    assert not any(isinstance(item.message, BrowserSecurityWarning) for item in captured)
    assert provider.policy.origins_unrestricted is False


def test_browser_origins_unrestricted_is_a_different_axis_than_is_permissive() -> None:
    """告警只认来源口径；审批配齐但来源全开时 is_permissive 仍为 False。"""
    assert BrowserPolicy().origins_unrestricted is True
    assert BrowserPolicy().is_permissive is True

    approvals_only = BrowserPolicy(require_approval_for=frozenset({"click"}))
    assert approvals_only.origins_unrestricted is True
    assert approvals_only.is_permissive is False

    wildcard = BrowserPolicy(
        allowed_origins=frozenset({"*"}), require_approval_for=frozenset({"click"})
    )
    assert wildcard.origins_unrestricted is True
    assert wildcard.is_permissive is False

    restricted = BrowserPolicy(allowed_origins=frozenset({"https://app.example.com"}))
    assert restricted.origins_unrestricted is False
    assert restricted.is_permissive is False


def test_desktop_secure_preset_requires_process_allowlist() -> None:
    with pytest.raises(ValueError):
        DesktopPolicy.secure([])
    policy = DesktopPolicy.secure(["123"], allowed_window_titles=["记事本"])
    assert policy.allowed_processes == frozenset({"123"})
    assert policy.allow_high_risk_controls is False
    assert policy.allow_geometry_click is False
    assert policy.is_permissive is False
    # PID 精确匹配（v0.27.1 收紧）：旧的 "123" 前缀放行 "1234" 语义已废弃。
    assert policy.allows_window("1", "记事本 - 未命名", "123") is True
    assert policy.allows_window("1", "记事本 - 未命名", "1234") is False
    assert policy.allows_window("2", "机密窗口", "9999") is False


def test_desktop_provider_warns_on_permissive_default() -> None:
    with pytest.warns(DesktopSecurityWarning):
        provider = DesktopProvider(_NoopBackend())  # type: ignore[arg-type]
    assert provider.policy.is_permissive is True

    secure = DesktopPolicy.secure(["123"])
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        DesktopProvider(_NoopBackend(), policy=secure)  # type: ignore[arg-type]
    assert not any(isinstance(item.message, DesktopSecurityWarning) for item in captured)

    # 仅标题白名单不构成安全边界：即便显式传了 policy，也要告警（v0.27.1 收紧）。
    title_only = DesktopPolicy(allowed_window_titles=frozenset({"记事本"}))
    with pytest.warns(DesktopSecurityWarning):
        DesktopProvider(_NoopBackend(), policy=title_only)  # type: ignore[arg-type]
