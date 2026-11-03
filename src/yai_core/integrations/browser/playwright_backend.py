"""可选 Playwright 后端：真实 Chromium 页面的观察与操作。

状态：**已实现 · 未在本仓库真机验证**（本机 CI 不安装 playwright，离线测试用
假后端覆盖 Provider 逻辑）。宿主接入方式::

    from yai_core.integrations.browser import BrowserPolicy, BrowserProvider, build_browser_tools
    from yai_core.integrations.browser.playwright_backend import PlaywrightBrowserBackend

    backend = PlaywrightBrowserBackend(headless=True)
    await backend.start()
    provider = BrowserProvider(backend, policy=BrowserPolicy(allowed_origins=frozenset({"https://example.com"})))
    core.register_tools(build_browser_tools(provider))

边界（诚实声明）：
- 只支持**顶层 frame**；``frame_id != "main"`` 直接报错，不做静默降级；
- 不处理下载 / 上传 / CAPTCHA / 反自动化绕过（任务书明确不作为目标）；
- 元素句柄用当页临时属性 ``data-yai-ref``；页面重渲染后旧引用按过期处理；
- 页面文本与标题原样回传，是否注入上下文由内核的不可信输出包裹负责。
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from yai_core.integrations.browser.models import ElementInfo, PageObservation, TabInfo
from yai_core.integrations.browser.policy import (
    BrowserBackendError,
    StaleElementRefError,
)

_COLLECT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],'
            + '[role=tab],[role=checkbox],[role=combobox],[role=menuitem],'
            + '[contenteditable=true]';
  const nodes = Array.from(document.querySelectorAll(sel));
  for (const el of nodes) {
    if (seen.has(el)) continue;
    seen.add(el);
    if (out.length >= 200) break;
    const idx = out.length;
    try { el.setAttribute('data-yai-ref', String(idx)); } catch (e) { continue; }
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const rawText = (el.innerText || el.value || el.getAttribute('aria-label')
                     || el.getAttribute('placeholder') || '').trim();
    out.push({
      ref: idx,
      tag: tag,
      role: el.getAttribute('role') || '',
      name: rawText.slice(0, 160),
      input_type: type,
      value: type === 'password' ? '' : String(el.value || '').slice(0, 160),
      disabled: !!el.disabled,
      sensitive: type === 'password',
    });
  }
  return out;
}
"""

_BODY_TEXT_JS = "() => (document.body ? document.body.innerText : '')"


def _fingerprint(url: str, title: str, text: str) -> str:
    digest = hashlib.sha1()  # noqa: S324 - 非安全用途，仅做页面状态摘要
    digest.update(url.encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update(title.encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update(text[:20000].encode("utf-8", "replace"))
    return digest.hexdigest()


class PlaywrightBrowserBackend:
    """用一个 Playwright Chromium 实例驱动"当前标签页"。"""

    def __init__(
        self,
        *,
        headless: bool = True,
        browser_name: str = "chromium",
        timeout_ms: int = 15_000,
        channel: str | None = None,
    ) -> None:
        if browser_name not in ("chromium", "firefox", "webkit"):
            raise ValueError("browser_name 只接受 chromium / firefox / webkit")
        if channel is not None and browser_name != "chromium":
            raise ValueError("channel 只对 chromium 生效（chrome / msedge 等）")
        self.headless = headless
        self.browser_name = browser_name
        self.timeout_ms = timeout_ms
        #: 使用系统安装的品牌浏览器（如 ``chrome`` / ``msedge``）而不是 Playwright
        #: 自带 build；None = 用 Playwright 下载的 Chromium。
        self.channel = channel
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - 仅在缺可选依赖时触发
            raise BrowserBackendError(
                "未安装 playwright；请 `pip install yai-agent-core[browser]`"
                " 并 `playwright install chromium`"
            ) from exc
        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, self.browser_name)
        launch_kwargs: dict[str, Any] = {"headless": self.headless}
        if self.channel is not None:
            launch_kwargs["channel"] = self.channel
        self._browser = await launcher.launch(**launch_kwargs)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:  # noqa: BLE001 - 关闭路径尽力而为
                    pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        self._playwright = self._browser = self._context = self._page = None

    def _require_page(self) -> Any:
        if self._page is None:
            raise BrowserBackendError("浏览器尚未启动；请先 await backend.start()")
        return self._page

    async def observe(self) -> PageObservation:
        page = self._require_page()
        try:
            raw_elements = await page.evaluate(_COLLECT_JS)
            text = await page.evaluate(_BODY_TEXT_JS)
            title = await page.title()
        except Exception as exc:  # noqa: BLE001 - 统一转成后端错误
            raise BrowserBackendError(f"观察页面失败：{type(exc).__name__}: {exc}") from exc
        elements = tuple(
            ElementInfo(
                backend_ref=str(item.get("ref", "")),
                role=str(item.get("role", "")),
                name=str(item.get("name", "")),
                tag=str(item.get("tag", "")),
                input_type=str(item.get("input_type", "")),
                value=str(item.get("value", "")),
                disabled=bool(item.get("disabled", False)),
                sensitive=bool(item.get("sensitive", False)),
            )
            for item in raw_elements
            if item.get("ref", "") != ""
        )
        tabs = self._tabs()
        return PageObservation(
            url=page.url,
            title=title,
            text=str(text or ""),
            elements=elements,
            tabs=tuple(tabs),
            tab_id=self._tab_id(page),
            fingerprint=_fingerprint(page.url, title, str(text or "")),
        )

    def _tabs(self) -> list[TabInfo]:
        if self._context is None:
            return []
        out: list[TabInfo] = []
        for index, page in enumerate(self._context.pages):
            out.append(
                TabInfo(
                    tab_id=f"tab-{index}",
                    url=page.url,
                    title="",
                    active=page is self._page,
                )
            )
        return out

    def _tab_id(self, page: Any) -> str:
        if self._context is None:
            return ""
        try:
            return f"tab-{self._context.pages.index(page)}"
        except ValueError:
            return ""

    def _locator(self, backend_ref: str, *, frame_id: str = "main") -> Any:
        if frame_id != "main":
            raise BrowserBackendError(
                f"暂不支持嵌套 frame 操作（frame_id={frame_id!r}）；只支持顶层 frame"
            )
        page = self._require_page()
        return page.locator(f'[data-yai-ref="{backend_ref}"]')

    async def _resolve_locator(self, backend_ref: str, frame_id: str) -> Any:
        locator = self._locator(backend_ref, frame_id=frame_id)
        try:
            count = await locator.count()
        except Exception as exc:  # noqa: BLE001
            raise BrowserBackendError(f"定位元素失败：{exc}") from exc
        if count == 0:
            raise StaleElementRefError(
                "元素已不在页面中（可能已重新渲染）；请重新观察后再操作"
            )
        return locator.first

    async def click(self, backend_ref: str, *, frame_id: str = "main") -> None:
        locator = await self._resolve_locator(backend_ref, frame_id)
        try:
            await locator.click()
        except Exception as exc:  # noqa: BLE001
            raise BrowserBackendError(f"点击失败：{type(exc).__name__}: {exc}") from exc

    async def type_text(
        self,
        backend_ref: str,
        text: str,
        *,
        frame_id: str = "main",
        submit: bool = False,
    ) -> None:
        locator = await self._resolve_locator(backend_ref, frame_id)
        try:
            await locator.fill(text)
            if submit:
                await locator.press("Enter")
        except Exception as exc:  # noqa: BLE001
            raise BrowserBackendError(f"输入失败：{type(exc).__name__}: {exc}") from exc

    async def select_option(
        self, backend_ref: str, value: str, *, frame_id: str = "main"
    ) -> None:
        locator = await self._resolve_locator(backend_ref, frame_id)
        try:
            await locator.select_option(value)
        except Exception as exc:  # noqa: BLE001
            raise BrowserBackendError(f"选择失败：{type(exc).__name__}: {exc}") from exc

    async def list_tabs(self) -> list[TabInfo]:
        self._require_page()
        return self._tabs()

    async def switch_tab(self, tab_id: str) -> PageObservation:
        self._require_page()
        try:
            index = int(str(tab_id).removeprefix("tab-"))
            page = self._context.pages[index]
        except (ValueError, IndexError, TypeError) as exc:
            raise BrowserBackendError(f"标签页不存在：{tab_id!r}") from exc
        self._page = page
        try:
            await page.bring_to_front()
        except Exception as exc:  # noqa: BLE001
            raise BrowserBackendError(f"切换标签页失败：{exc}") from exc
        return await self.observe()

    async def wait_for(
        self,
        *,
        url_contains: str | None = None,
        text_contains: str | None = None,
        timeout_ms: int = 10_000,
    ) -> bool:
        page = self._require_page()
        deadline = asyncio.get_running_loop().time() + max(1, timeout_ms) / 1000
        while True:
            url = page.url
            if url_contains and url_contains in url:
                return True
            if text_contains:
                try:
                    text = await page.evaluate(_BODY_TEXT_JS)
                except Exception:  # noqa: BLE001 - 导航中页面可能暂时不可读
                    text = ""
                if text_contains in str(text or ""):
                    return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def open_url(self, url: str) -> PageObservation:
        page = self._require_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            raise BrowserBackendError(f"导航失败：{type(exc).__name__}: {exc}") from exc
        return await self.observe()
