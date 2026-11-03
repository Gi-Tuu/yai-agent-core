"""Browser Provider 离线测试（v0.26 Batch 2）：观察 / 快照引用 / 动作 / 治理。

全部用假后端，不启动浏览器、不联网。覆盖：
1. 观察生成带版本的快照引用（s<N>:<index>），文本有上限且可截断；
2. 最新快照之外的引用一律判过期，后端不会被调用；
3. 动作返回 executed + changed / no_observable_change / unknown，绝不直接宣称成功；
4. 密码框默认拒绝自动填写；元素值不回显；
5. 来源白名单：禁止的页面连文本/元素都不回传，动作与导航被拦；
6. 工具经 ToolRegistry + ToolExecutor 走统一治理，产生收据事件；
7. tabs / switch_tab / wait / open 的最小闭环。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from yai_core.channels import CollectChannel
from yai_core.integrations.browser import (
    BrowserBackendError,
    BrowserPolicy,
    BrowserProvider,
    ElementInfo,
    PageObservation,
    TabInfo,
    build_browser_tools,
)
from yai_core.policy import AllowlistPolicy
from yai_core.tools import ToolExecutor, ToolRegistry


class FakeBackend:
    """可编程的假浏览器：页面状态、元素和动作记录都在内存里。"""

    def __init__(
        self,
        *,
        url: str = "https://allowed.test/form",
        title: str = "表单",
        text: str = "请填写表单",
        elements: tuple[ElementInfo, ...] | None = None,
        tabs: tuple[TabInfo, ...] | None = None,
        tab_id: str = "tab-0",
    ) -> None:
        self.url = url
        self.title = title
        self.text = text
        self.elements = elements or (
            ElementInfo(backend_ref="b0", role="button", name="提交", tag="button"),
            ElementInfo(
                backend_ref="b1", role="textbox", name="搜索", tag="input", input_type="text"
            ),
            ElementInfo(
                backend_ref="b2",
                role="textbox",
                name="密码",
                tag="input",
                input_type="password",
                sensitive=True,
            ),
            ElementInfo(backend_ref="b3", role="combobox", name="城市", tag="select"),
        )
        self.tabs = tabs or (TabInfo(tab_id=tab_id, url=url, title=title, active=True),)
        self.tab_id = tab_id
        self.fingerprint = "f1"
        self.clicked: list[str] = []
        self.typed: list[tuple[str, str, bool]] = []
        self.selected: list[tuple[str, str]] = []
        self.opened: list[str] = []
        self.observe_fail = False
        self.change_on_action = True

    def observation(self) -> PageObservation:
        return PageObservation(
            url=self.url,
            title=self.title,
            text=self.text,
            elements=self.elements,
            tabs=self.tabs,
            tab_id=self.tab_id,
            fingerprint=self.fingerprint,
        )

    async def observe(self) -> PageObservation:
        if self.observe_fail:
            raise BrowserBackendError("页面已关闭")
        return self.observation()

    async def click(self, backend_ref: str, *, frame_id: str = "main") -> None:
        self.clicked.append(backend_ref)
        if self.change_on_action:
            self.fingerprint = "f2"
            self.text = "已提交"

    async def type_text(
        self, backend_ref: str, text: str, *, frame_id: str = "main", submit: bool = False
    ) -> None:
        self.typed.append((backend_ref, text, submit))

    async def select_option(
        self, backend_ref: str, value: str, *, frame_id: str = "main"
    ) -> None:
        self.selected.append((backend_ref, value))

    async def list_tabs(self) -> list[TabInfo]:
        return list(self.tabs)

    async def switch_tab(self, tab_id: str) -> PageObservation:
        self.tab_id = tab_id
        return self.observation()

    async def wait_for(
        self,
        *,
        url_contains: str | None = None,
        text_contains: str | None = None,
        timeout_ms: int = 10_000,
    ) -> bool:
        if url_contains and url_contains in self.url:
            return True
        if text_contains and text_contains in self.text:
            return True
        return False

    async def open_url(self, url: str) -> PageObservation:
        self.opened.append(url)
        self.url = url
        return self.observation()


def _provider(backend: FakeBackend, **policy_kw: Any) -> BrowserProvider:
    return BrowserProvider(backend, policy=BrowserPolicy(**policy_kw))


def _run(coro):
    return asyncio.run(coro)


# ---------- 观察与快照引用 ----------


def test_observe_builds_versioned_refs() -> None:
    provider = _provider(FakeBackend())
    result = _run(provider.observe())

    assert result["ok"] is True
    assert result["snapshot_id"] == "s1"
    refs = [element["ref"] for element in result["elements"]]
    assert refs == ["s1:0", "s1:1", "s1:2", "s1:3"]
    assert result["origin"] == "https://allowed.test"
    assert result["tabs"][0]["tab_id"] == "tab-0"


def test_observe_truncates_text_and_elements() -> None:
    backend = FakeBackend(
        text="长" * 50,
        elements=tuple(
            ElementInfo(backend_ref=f"b{i}", role="button", name=f"按钮{i}")
            for i in range(10)
        ),
    )
    provider = BrowserProvider(backend, policy=BrowserPolicy(max_text_chars=20, max_elements=3))

    result = _run(provider.observe())

    assert result["text"] == "长" * 20
    assert result["text_truncated"] is True
    assert len(result["elements"]) == 3
    assert result["elements_truncated"] is True


def test_password_value_never_returned() -> None:
    backend = FakeBackend(
        elements=(
            ElementInfo(
                backend_ref="p0",
                role="textbox",
                name="密码",
                input_type="password",
                value="hunter2",
                sensitive=True,
            ),
        )
    )
    result = _run(_provider(backend).observe())
    public = result["elements"][0]
    assert public["sensitive"] is True
    assert "value" not in public


# ---------- 过期引用 ----------


def test_stale_ref_rejected_without_calling_backend() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    _run(provider.observe())
    _run(provider.observe())  # 第二次观察让 s1 引用过期

    result = _run(provider.click("s1:0"))

    assert result["ok"] is False
    assert result["status"] == "stale_ref"
    assert backend.clicked == []


def test_click_before_any_observation_is_rejected() -> None:
    backend = FakeBackend()
    result = _run(_provider(backend).click("s0:0"))
    assert result["status"] == "stale_ref"
    assert backend.clicked == []


# ---------- 动作语义 ----------


def test_click_reports_executed_and_observed_change() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    _run(provider.observe())

    result = _run(provider.click("s1:0"))

    assert backend.clicked == ["b0"]
    assert result["ok"] is True
    assert result["status"] == "executed"
    assert result["outcome"] == "changed"
    assert result["snapshot_id"] == "s2"  # 动作后自动重新观察


def test_click_without_observable_change_is_not_success_claim() -> None:
    backend = FakeBackend()
    backend.change_on_action = False
    provider = _provider(backend)
    _run(provider.observe())

    result = _run(provider.click("s1:0"))

    assert result["status"] == "executed"
    assert result["outcome"] == "no_observable_change"
    assert result["changed"] is False
    # 没有任何字段宣称任务成功 / 业务完成。
    assert "success" not in result


def test_click_unknown_when_reobserve_fails() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    _run(provider.observe())
    backend.observe_fail = True

    result = _run(provider.click("s1:0"))

    assert result["status"] == "executed"
    assert result["outcome"] == "unknown"


def test_type_sensitive_field_blocked_by_default() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    _run(provider.observe())

    result = _run(provider.type_text("s1:2", "secret"))

    assert result["status"] == "sensitive_field_blocked"
    assert backend.typed == []


def test_type_sensitive_field_allowed_when_host_opts_in() -> None:
    backend = FakeBackend()
    provider = _provider(backend, allow_password_fields=True)
    _run(provider.observe())

    result = _run(provider.type_text("s1:2", "secret"))

    assert result["status"] == "executed"
    assert backend.typed == [("b2", "secret", False)]


def test_type_passes_submit_flag_and_select_works() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    _run(provider.observe())

    typed = _run(provider.type_text("s1:1", "周报", submit=True))
    selected = _run(provider.select("s2:3", "shanghai"))

    assert typed["status"] == "executed"
    assert backend.typed == [("b1", "周报", True)]
    assert selected["status"] == "executed"
    assert backend.selected == [("b3", "shanghai")]


# ---------- 来源边界 ----------


def test_observe_on_disallowed_origin_returns_no_content() -> None:
    backend = FakeBackend(url="https://evil.test/page", text="机密页面内容")
    provider = _provider(backend, allowed_origins=frozenset({"https://allowed.test"}))

    result = _run(provider.observe())

    assert result["ok"] is False
    assert result["status"] == "origin_not_allowed"
    assert "text" not in result and "elements" not in result


def test_actions_blocked_when_current_origin_not_allowed() -> None:
    backend = FakeBackend(url="https://allowed.test/page")
    provider = _provider(backend, allowed_origins=frozenset({"https://allowed.test"}))
    _run(provider.observe())
    backend.url = "https://evil.test/page"  # 页面自己跳走

    result = _run(provider.click("s1:0"))

    assert result["status"] == "origin_not_allowed"
    assert backend.clicked == []


def test_open_disallowed_origin_rejected_before_backend() -> None:
    backend = FakeBackend()
    provider = _provider(backend, allowed_origins=frozenset({"https://allowed.test"}))

    result = _run(provider.open("https://evil.test"))

    assert result["status"] == "origin_not_allowed"
    assert backend.opened == []


def test_open_tool_gate_rejects_before_handler() -> None:
    backend = FakeBackend()
    provider = _provider(backend, allowed_origins=frozenset({"https://allowed.test"}))
    tools = {spec.name: spec for spec in build_browser_tools(provider)}

    decision = tools["browser_open"].governance.gate({"url": "https://evil.test"}, None)  # type: ignore[misc]
    allowed = tools["browser_open"].governance.gate({"url": "https://allowed.test"}, None)  # type: ignore[misc]

    assert decision.reject
    assert allowed.reject == ""


def test_switch_tab_to_disallowed_origin_returns_metadata_only() -> None:
    backend = FakeBackend(
        tabs=(
            TabInfo(tab_id="tab-0", url="https://allowed.test/a", active=True),
            TabInfo(tab_id="tab-1", url="https://evil.test/b"),
        )
    )
    provider = _provider(backend, allowed_origins=frozenset({"https://allowed.test"}))
    backend.url = "https://evil.test/b"

    result = _run(provider.switch_tab("tab-1"))

    assert result["status"] == "origin_not_allowed"
    assert "text" not in result


# ---------- 标签页 / 等待 ----------


def test_tabs_and_switch_tab() -> None:
    backend = FakeBackend(
        tabs=(
            TabInfo(tab_id="tab-0", url="https://allowed.test/a", title="A", active=True),
            TabInfo(tab_id="tab-1", url="https://allowed.test/b", title="B"),
        )
    )
    provider = _provider(backend)

    listed = _run(provider.list_tabs())
    switched = _run(provider.switch_tab("tab-1"))

    assert [tab["tab_id"] for tab in listed["tabs"]] == ["tab-0", "tab-1"]
    assert switched["ok"] is True
    assert switched["snapshot_id"] == "s1"


def test_wait_refreshes_snapshot_and_reports_match() -> None:
    backend = FakeBackend(url="https://allowed.test/done", text="完成")
    provider = _provider(backend)
    _run(provider.observe())

    matched = _run(provider.wait(text_contains="完成"))
    missed = _run(provider.wait(text_contains="不存在的文字", timeout_ms=50))

    assert matched["matched"] is True
    assert matched["snapshot"]["snapshot_id"] == "s2"
    assert missed["matched"] is False
    assert missed["snapshot"]["snapshot_id"] == "s3"


def test_wait_requires_a_condition() -> None:
    provider = _provider(FakeBackend())
    result = _run(provider.wait())
    assert result["status"] == "invalid_arguments"


# ---------- 统一治理：经过 ToolExecutor ----------


def _executor(provider: BrowserProvider) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_many(build_browser_tools(provider))
    return ToolExecutor(registry, AllowlistPolicy((), mode="auto"), CollectChannel())


def test_tools_go_through_executor_and_emit_receipts() -> None:
    backend = FakeBackend()
    provider = _provider(backend)
    executor = _executor(provider)

    events, ok, text = _run(executor.execute("browser_observe", {}))

    assert ok is True
    assert "snapshot_id" in text
    result_events = [event for event in events if event.type == "tool_result"]
    assert result_events and "receipt" in result_events[-1].data


def test_type_tool_declares_text_sensitive_for_audit() -> None:
    provider = _provider(FakeBackend())
    tools = {spec.name: spec for spec in build_browser_tools(provider)}
    assert tools["browser_type"].governance.sensitive == ("text",)


def test_approval_declaration_follows_policy() -> None:
    provider = _provider(FakeBackend(), require_approval_for=frozenset({"click"}))
    tools = {spec.name: spec for spec in build_browser_tools(provider)}
    approval = tools["browser_click"].governance.approval
    assert approval is not None and approval.required is True
    assert tools["browser_select"].governance.approval is None


def test_executor_rejects_open_gate_before_handler() -> None:
    backend = FakeBackend()
    provider = _provider(backend, allowed_origins=frozenset({"https://allowed.test"}))
    executor = _executor(provider)

    _events, ok, _text = _run(executor.execute("browser_open", {"url": "https://evil.test"}))

    assert ok is False
    assert backend.opened == []


@pytest.mark.parametrize("bad_policy", [{"max_text_chars": 0}, {"rate_limit_per_minute": -1}])
def test_invalid_policy_rejected(bad_policy: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        BrowserPolicy(**bad_policy)
