"""PlaywrightBrowserBackend 的离线回归：用假 playwright SDK 覆盖真实后端代码路径。

不安装 playwright、不启动浏览器；验证 start/observe/动作/标签页/等待/关闭与 ref 过期。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from yai_core.integrations.browser.playwright_backend import PlaywrightBrowserBackend
from yai_core.integrations.browser.policy import (
    BrowserBackendError,
    StaleElementRefError,
)


class FakeLocator:
    def __init__(self, page: FakePage, ref: str) -> None:
        self.page = page
        self.ref = ref

    async def count(self) -> int:
        return 0 if self.page.stale else 1

    @property
    def first(self) -> FakeLocator:
        return self

    async def click(self) -> None:
        self.page.clicks.append(self.ref)

    async def fill(self, text: str) -> None:
        self.page.fills.append((self.ref, text))

    async def press(self, key: str) -> None:
        self.page.presses.append(key)

    async def select_option(self, value: str) -> None:
        self.page.selects.append((self.ref, value))


class FakePage:
    def __init__(self, url: str = "https://allowed.test/page", text: str = "正文") -> None:
        self.url = url
        self.text = text
        self.stale = False
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.presses: list[str] = []
        self.selects: list[tuple[str, str]] = []
        self.fronted = 0

    async def title(self) -> str:
        return "假页面"

    async def evaluate(self, js: str):  # noqa: ANN001 - 假 SDK
        if "querySelectorAll" in js:
            return [
                {
                    "ref": 0,
                    "tag": "button",
                    "role": "button",
                    "name": "提交",
                    "input_type": "",
                    "value": "",
                    "disabled": False,
                    "sensitive": False,
                },
                {
                    "ref": 1,
                    "tag": "input",
                    "role": "textbox",
                    "name": "密码",
                    "input_type": "password",
                    "value": "secret",
                    "disabled": False,
                    "sensitive": True,
                },
            ]
        if "document.body" in js:
            return self.text
        return []

    def set_default_timeout(self, value: int) -> None:
        self.default_timeout = value

    def locator(self, selector: str) -> FakeLocator:
        ref = selector.split('"')[1]
        return FakeLocator(self, ref)

    async def bring_to_front(self) -> None:
        self.fronted += 1

    async def goto(self, url: str, *, wait_until: str = "") -> None:
        self.url = url
        self.text = f"已导航到 {url}"


class FakeContext:
    def __init__(self) -> None:
        self.pages: list[FakePage] = [FakePage()]
        self.closed = False
        self.routes: list[tuple[str, object]] = []

    async def route(self, pattern: str, handler: object) -> None:
        self.routes.append((pattern, handler))

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.context = FakeContext()
        self.closed = False

    async def new_context(self) -> FakeContext:
        return self.context

    async def close(self) -> None:
        self.closed = True


class FakeLauncher:
    def __init__(self) -> None:
        self.launch_kwargs: dict = {}
        self.browser = FakeBrowser()

    async def launch(self, **kwargs):  # noqa: ANN003 - 假 SDK
        self.launch_kwargs = kwargs
        return self.browser


class FakeManager:
    def __init__(self) -> None:
        self.chromium = FakeLauncher()
        self.stopped = False

    async def start(self) -> FakeManager:
        return self

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def fake_playwright(monkeypatch: pytest.MonkeyPatch) -> FakeManager:
    manager = FakeManager()
    package = types.ModuleType("playwright")
    package.__path__ = []  # type: ignore[attr-defined]
    async_api = types.ModuleType("playwright.async_api")

    def async_playwright() -> FakeManager:
        return manager

    async_api.async_playwright = async_playwright  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    return manager


def test_backend_start_observe_and_actions(fake_playwright: FakeManager) -> None:
    backend = PlaywrightBrowserBackend(headless=True, channel="chrome", timeout_ms=1234)
    asyncio.run(backend.start())

    assert fake_playwright.chromium.launch_kwargs == {
        "headless": True,
        "channel": "chrome",
    }
    observation = asyncio.run(backend.observe())
    assert observation.url == "https://allowed.test/page"
    assert observation.title == "假页面"
    assert observation.text == "正文"
    assert [element.backend_ref for element in observation.elements] == ["0", "1"]
    assert observation.elements[1].sensitive is True
    assert observation.fingerprint

    asyncio.run(backend.click("0"))
    asyncio.run(backend.type_text("0", "hello", submit=True))
    asyncio.run(backend.select_option("0", "v1"))
    page = backend._page  # noqa: SLF001 - 断言假 SDK 收到的动作
    assert page.clicks == ["0"]
    assert page.fills == [("0", "hello")]
    assert page.presses == ["Enter"]
    assert page.selects == [("0", "v1")]

    asyncio.run(backend.close())
    assert fake_playwright.stopped is True


def test_backend_stale_ref_and_tabs(fake_playwright: FakeManager) -> None:
    backend = PlaywrightBrowserBackend()
    asyncio.run(backend.start())
    backend._page.stale = True  # noqa: SLF001
    with pytest.raises(StaleElementRefError):
        asyncio.run(backend.click("0"))

    tabs = asyncio.run(backend.list_tabs())
    active = [tab for tab in tabs if tab.active]
    assert len(tabs) == 2 and active and active[0].tab_id == "tab-1"
    asyncio.run(backend.switch_tab("tab-0"))
    assert backend._page.fronted == 1  # noqa: SLF001
    with pytest.raises(BrowserBackendError):
        asyncio.run(backend.switch_tab("tab-99"))
    asyncio.run(backend.close())


def test_backend_wait_and_open(fake_playwright: FakeManager) -> None:
    backend = PlaywrightBrowserBackend()
    asyncio.run(backend.start())
    assert asyncio.run(backend.wait_for(url_contains="allowed", timeout_ms=50)) is True
    assert asyncio.run(backend.wait_for(text_contains="不存在", timeout_ms=30)) is False

    observation = asyncio.run(backend.open_url("https://allowed.test/next"))
    assert observation.url == "https://allowed.test/next"
    assert "next" in observation.text
    asyncio.run(backend.close())


def test_backend_requires_start_before_use(fake_playwright: FakeManager) -> None:
    backend = PlaywrightBrowserBackend()
    with pytest.raises(BrowserBackendError):
        asyncio.run(backend.observe())
    assert fake_playwright.chromium.launch_kwargs == {}
