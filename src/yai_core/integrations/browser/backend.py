"""浏览器后端的契约（Provider 只依赖这个 Protocol，不知道 Playwright / 扩展）。

真实实现见 :mod:`yai_core.integrations.browser.playwright_backend`（可选依赖，
未安装时不影响内核）；测试 / 宿主自建实现直接满足该协议即可。所有方法都是
``async``，异常请抛 :class:`BrowserError` 的子类或原样抛出——Provider 会把
可恢复错误转成结构化结果，把未知异常留给执行器记账。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from yai_core.integrations.browser.models import PageObservation, TabInfo


@runtime_checkable
class BrowserBackend(Protocol):
    """一个可被 Provider 驱动的浏览器（真实浏览器 / 扩展 / 假实现）。"""

    async def observe(self) -> PageObservation:
        """观察当前活动页：URL、标题、可见文本、可交互元素、标签页与指纹。"""
        ...

    async def click(self, backend_ref: str, *, frame_id: str = "main") -> None:
        """点击一个元素；元素已不存在时抛 StaleElementRefError。"""
        ...

    async def type_text(
        self,
        backend_ref: str,
        text: str,
        *,
        frame_id: str = "main",
        submit: bool = False,
    ) -> None:
        """向输入框填入文本；``submit=True`` 时随后回车 / 提交。"""
        ...

    async def select_option(
        self, backend_ref: str, value: str, *, frame_id: str = "main"
    ) -> None:
        """选择一个下拉项。"""
        ...

    async def list_tabs(self) -> list[TabInfo]:
        """列出当前浏览器窗口的标签页。"""
        ...

    async def switch_tab(self, tab_id: str) -> PageObservation:
        """切换到指定标签页并返回其观察结果。"""
        ...

    async def wait_for(
        self,
        *,
        url_contains: str | None = None,
        text_contains: str | None = None,
        timeout_ms: int = 10_000,
    ) -> bool:
        """等待条件成立；超时返回 False（不抛异常）。"""
        ...

    async def open_url(self, url: str) -> PageObservation:
        """在当前标签页导航到目标 URL。"""
        ...
