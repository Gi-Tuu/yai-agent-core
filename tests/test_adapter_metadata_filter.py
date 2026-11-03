"""适配器元数据过滤：被策略禁止的 tabs / windows 不得把 URL/标题/进程泄漏给模型。"""

from __future__ import annotations

import asyncio

from yai_core.integrations.browser import (
    BrowserPolicy,
    BrowserProvider,
    TabInfo,
)
from yai_core.integrations.desktop import (
    DesktopPolicy,
    DesktopProvider,
    WindowInfo,
)


class _BrowserBackend:
    async def list_tabs(self) -> list[TabInfo]:
        return [
            TabInfo(tab_id="tab-0", url="https://allowed.test/a", title="允许页"),
            TabInfo(tab_id="tab-1", url="https://evil.test/secret", title="机密页"),
        ]


class _DesktopBackend:
    async def list_windows(self) -> list[WindowInfo]:
        return [
            WindowInfo(window_id="1", title="允许窗口", process_id="100"),
            WindowInfo(window_id="2", title="机密窗口", process_id="200"),
        ]


def test_browser_list_tabs_filters_disallowed_origins() -> None:
    provider = BrowserProvider(
        _BrowserBackend(),  # type: ignore[arg-type]
        policy=BrowserPolicy(allowed_origins=frozenset({"https://allowed.test"})),
    )
    result = asyncio.run(provider.list_tabs())
    assert result["ok"] is True
    assert [tab["url"] for tab in result["tabs"]] == ["https://allowed.test/a"]
    assert result["hidden_count"] == 1
    assert "evil" not in str(result)
    assert "机密页" not in str(result)


def test_desktop_list_windows_filters_disallowed_windows() -> None:
    provider = DesktopProvider(
        _DesktopBackend(),  # type: ignore[arg-type]
        policy=DesktopPolicy(allowed_processes=frozenset({"100"})),
    )
    result = asyncio.run(provider.list_windows())
    assert result["ok"] is True
    assert [window["title"] for window in result["windows"]] == ["允许窗口"]
    assert result["hidden_count"] == 1
    assert "机密窗口" not in str(result)
    assert "200" not in str(result)
