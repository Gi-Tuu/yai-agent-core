"""Playwright 后端的参数校验（不启动浏览器，CI 可跑）。"""

from __future__ import annotations

import pytest

from yai_core.integrations.browser.playwright_backend import PlaywrightBrowserBackend
from yai_core.sidecar.native_bridge import BridgeConfig, BridgeOriginError


def test_channel_defaults_to_none() -> None:
    backend = PlaywrightBrowserBackend()
    assert backend.channel is None


def test_channel_accepted_for_chromium() -> None:
    backend = PlaywrightBrowserBackend(channel="chrome")
    assert backend.channel == "chrome"


def test_channel_rejected_for_non_chromium() -> None:
    with pytest.raises(ValueError):
        PlaywrightBrowserBackend(browser_name="firefox", channel="chrome")


def test_invalid_browser_name_rejected() -> None:
    with pytest.raises(ValueError):
        PlaywrightBrowserBackend(browser_name="opera")


def test_origin_and_bridge_config_guard() -> None:
    """Bridge 的来源校验仍是 fail closed（真机脚本依赖这一行为）。"""
    with pytest.raises(BridgeOriginError):
        BridgeConfig(allowed_origins=frozenset()).check_origin("chrome-extension://abc")
