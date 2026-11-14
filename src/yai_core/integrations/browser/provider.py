"""Browser Provider：把"观察 → 引用 → 动作 → 再观察"做成一组受治理的工具。

设计要点（对应 v0.26 任务书 4.2–4.4）：

- **引用带快照版本**：每次观察生成 ``s<N>`` 快照，元素引用形如 ``s3:2``。动作只
  接受**最新快照**里的引用，页面重渲染后旧引用直接判过期并要求重新观察——绝不
  拿旧坐标 / 旧节点盲目操作。
- **"调用成功"≠"业务成功"**：动作返回 ``status=executed`` 与
  ``outcome=changed / no_observable_change / unknown``，从不声称任务已完成；
  真正的任务验收由 Outcome Verification 契约（Batch 3）负责。
- **不另起 Tool Bus**：Provider 只造 ``ToolSpec``，注册进内核 ``ToolRegistry``
  后统一走 ``ToolExecutor`` 的参数校验、权限策略、审批、收据与事件。
- **来源边界**：观察与动作都校验当前页面来源；跨域落点会标
  ``origin_allowed=False`` 并阻止后续动作。
"""

from __future__ import annotations

import warnings
from typing import Any

from yai_core.diagnostics import DiagnosticSink
from yai_core.integrations.browser.backend import BrowserBackend
from yai_core.integrations.browser.models import ElementInfo, PageObservation, Snapshot
from yai_core.integrations.browser.policy import (
    BrowserError,
    BrowserPolicy,
    BrowserPolicyError,
    BrowserSecurityWarning,
    StaleElementRefError,
    origin_of,
)
from yai_core.types import GateDecision, ToolGovernance, ToolSpec

#: 公开工具名（前缀固定，方便宿主按前缀做权限分组）。
OBSERVE = "browser_observe"
CLICK = "browser_click"
TYPE = "browser_type"
SELECT = "browser_select"
TABS = "browser_tabs"
SWITCH_TAB = "browser_switch_tab"
WAIT = "browser_wait"
OPEN = "browser_open"

_BROWSER_TOOL_NAMES = (OBSERVE, CLICK, TYPE, SELECT, TABS, SWITCH_TAB, WAIT, OPEN)


class BrowserProvider:
    """受治理的浏览器工具集合；一个实例对应一个可控浏览器窗口。

    Args:
        backend: 满足 :class:`BrowserBackend` 契约的后端（Playwright / 扩展 / 假实现）。
        policy: 来源白名单、敏感字段、审批与频率约束；默认宽松但可显式收紧。
            未传、或传入但来源白名单为空 / 含 ``*`` 时会发 ``BrowserSecurityWarning``。
        on_diagnostic: 可选本地诊断回调（快照过期、来源拒绝等）。
    """

    def __init__(
        self,
        backend: BrowserBackend,
        *,
        policy: BrowserPolicy | None = None,
        on_diagnostic: DiagnosticSink | None = None,
    ) -> None:
        if not isinstance(policy, BrowserPolicy) and policy is not None:
            raise ValueError("policy 必须是 BrowserPolicy 实例或 None")
        if policy is None:
            warnings.warn(
                "BrowserProvider 未显式传入 BrowserPolicy，使用开发/测试宽松默认"
                "（来源不限制、修改性动作不额外审批）；生产请用 "
                "BrowserPolicy.secure([...]) 显式列出来源。",
                BrowserSecurityWarning,
                stacklevel=2,
            )
            policy = BrowserPolicy.development()
        elif policy.origins_unrestricted:
            warnings.warn(
                "BrowserPolicy 没有来源白名单（allowed_origins 为空或含 '*'），"
                "来源闸不构成安全边界；生产请用 BrowserPolicy.secure([...]) "
                "显式列出来源。",
                BrowserSecurityWarning,
                stacklevel=2,
            )
        self.backend = backend
        self.policy = policy
        # 可选后端能力：在导航/重定向阶段就拦截未授权文档请求，而不是等最终页面。
        setter = getattr(backend, "set_origin_guard", None)
        if callable(setter):
            setter(policy.allows_origin)
        self.on_diagnostic = on_diagnostic
        self._version = 0
        self._snapshot: Snapshot | None = None
        #: 最近一次动作的结果（可审计；不替代执行收据）。
        self.last_action: dict[str, Any] | None = None

    # ---------- 观察 ----------

    @property
    def snapshot_id(self) -> str | None:
        return self._snapshot.snapshot_id if self._snapshot else None

    async def observe(self) -> dict[str, Any]:
        """观察当前页面并开启新快照；旧快照的元素引用随即失效。"""
        try:
            raw = await self.backend.observe()
        except BrowserError as exc:
            return {"ok": False, "status": "backend_error", "error": str(exc)}
        if not isinstance(raw, PageObservation):
            return {
                "ok": False,
                "status": "backend_error",
                "error": "backend.observe() 必须返回 PageObservation",
            }
        if not self.policy.allows_origin(raw.url):
            # 不允许的来源：连文本与元素都不回传，避免把不该读的页面读进模型。
            self._adopt_empty(raw)
            return {
                "ok": False,
                "status": "origin_not_allowed",
                "snapshot_id": self.snapshot_id,
                "origin": origin_of(raw.url),
                "url": raw.url,
                "hint": "当前页面来源不在宿主允许的范围内；请不要继续读取或操作它。",
            }
        return {"ok": True, "status": "observed", **self._adopt(raw)}

    # ---------- 动作 ----------

    async def click(self, ref: str) -> dict[str, Any]:
        return await self._act("click", ref, lambda element: self._click(element))

    async def type_text(self, ref: str, text: str, *, submit: bool = False) -> dict[str, Any]:
        return await self._act(
            "type", ref, lambda element: self._type(element, text, submit=submit)
        )

    async def select(self, ref: str, value: str) -> dict[str, Any]:
        return await self._act("select", ref, lambda element: self._select(element, value))

    async def list_tabs(self) -> dict[str, Any]:
        try:
            tabs = await self.backend.list_tabs()
        except BrowserError as exc:
            return {"ok": False, "status": "backend_error", "error": str(exc)}
        # 元数据读取也受来源白名单约束：被禁标签页的 URL/标题不得进入模型上下文。
        visible = [tab for tab in tabs if self.policy.allows_origin(tab.url)]
        return {
            "ok": True,
            "status": "observed",
            "tabs": [tab.to_public() for tab in visible],
            "hidden_count": len(tabs) - len(visible),
        }

    async def switch_tab(self, tab_id: str) -> dict[str, Any]:
        try:
            raw = await self.backend.switch_tab(tab_id)
        except BrowserError as exc:
            return {"ok": False, "status": "backend_error", "error": str(exc)}
        if not self.policy.allows_origin(raw.url):
            self._adopt_empty(raw)
            return {
                "ok": False,
                "status": "origin_not_allowed",
                "snapshot_id": self.snapshot_id,
                "origin": origin_of(raw.url),
            }
        adopted = self._adopt(raw)
        return {"ok": True, "status": "executed", "outcome": "changed", **adopted}

    async def wait(
        self,
        *,
        url_contains: str | None = None,
        text_contains: str | None = None,
        timeout_ms: int = 10_000,
    ) -> dict[str, Any]:
        if not (url_contains or text_contains):
            return {
                "ok": False,
                "status": "invalid_arguments",
                "hint": "至少给出 url_contains 或 text_contains 之一",
            }
        try:
            matched = await self.backend.wait_for(
                url_contains=url_contains,
                text_contains=text_contains,
                timeout_ms=max(1, int(timeout_ms)),
            )
        except BrowserError as exc:
            return {"ok": False, "status": "backend_error", "error": str(exc)}
        # 无论是否命中都重新观察一次：让模型拿到新快照，避免继续用旧引用。
        adopted = await self.observe()
        return {
            "ok": bool(matched),
            "status": "waited",
            "matched": bool(matched),
            "snapshot": adopted,
        }

    async def open(self, url: str) -> dict[str, Any]:
        try:
            self.policy.check_origin(url, action="open")
        except BrowserPolicyError as exc:
            return {"ok": False, "status": "origin_not_allowed", "error": str(exc)}
        try:
            raw = await self.backend.open_url(url)
        except BrowserError as exc:
            return {"ok": False, "status": "backend_error", "error": str(exc)}
        if not self.policy.allows_origin(raw.url):
            self._adopt_empty(raw)
            return {
                "ok": False,
                "status": "origin_not_allowed",
                "snapshot_id": self.snapshot_id,
                "origin": origin_of(raw.url),
            }
        adopted = self._adopt(raw)
        return {"ok": True, "status": "executed", "outcome": "changed", **adopted}

    # ---------- 内部 ----------

    def _resolve(self, ref: str) -> ElementInfo:
        if self._snapshot is None:
            raise StaleElementRefError("尚未观察页面；请先调用 browser_observe")
        version, _, index_part = (ref or "").partition(":")
        expected = f"s{self._snapshot.version}"
        if version != expected:
            raise StaleElementRefError(
                f"元素引用 {ref!r} 来自旧快照；最新快照是 {expected}，请重新观察"
            )
        try:
            index = int(index_part)
        except ValueError as exc:
            raise StaleElementRefError(f"元素引用格式非法：{ref!r}") from exc
        element = self._snapshot.observation.element_by_index(index)
        if element is None:
            raise StaleElementRefError(f"元素引用 {ref!r} 不存在于最新快照，请重新观察")
        return element

    async def _act(self, action: str, ref: str, call: Any) -> dict[str, Any]:
        try:
            element = self._resolve(ref)
        except StaleElementRefError as exc:
            return {
                "ok": False,
                "status": "stale_ref",
                "error": str(exc),
                "snapshot_id": self.snapshot_id,
                "hint": "请重新调用 browser_observe 获取新引用后再操作。",
            }
        assert self._snapshot is not None
        current_url = self._snapshot.observation.url
        if self.policy.allowed_origins:
            # 只读取当前 URL，不做 observe：observe 会重写 data-yai-ref，
            # 让刚通过校验的 ref 指向另一个元素（安全边界与执行目标错位）。
            current_url_getter = getattr(self.backend, "current_url", None)
            if callable(current_url_getter):
                try:
                    current_url = await current_url_getter()
                except BrowserError as exc:
                    return {"ok": False, "status": "backend_error", "error": str(exc)}
            else:
                # 自定义/旧 Backend 没有 current_url：退回观察一次。Playwright
                # 后端已提供 current_url，因此生产路径不会触发 ref 重排。
                try:
                    peek = await self.backend.observe()
                except BrowserError as exc:
                    return {"ok": False, "status": "backend_error", "error": str(exc)}
                current_url = peek.url
        if not self.policy.allows_origin(current_url):
            return {
                "ok": False,
                "status": "origin_not_allowed",
                "origin": origin_of(current_url),
            }
        if element.disabled:
            return {"ok": False, "status": "element_disabled", "ref": ref}
        if (
            action == "type"
            and element.sensitive
            and not self.policy.allow_password_fields
        ):
            return {
                "ok": False,
                "status": "sensitive_field_blocked",
                "ref": ref,
                "hint": "该字段被判定为敏感输入（如密码框）；宿主未授权自动填写。",
            }
        previous = self._snapshot.observation.fingerprint
        try:
            await call(element)
        except BrowserError as exc:
            result = {
                "ok": False,
                "status": "backend_error",
                "error": str(exc),
                "snapshot_id": self.snapshot_id,
            }
            self.last_action = {"action": action, **result}
            return result
        result = await self._settle(action, previous)
        self.last_action = result
        return result

    async def _settle(self, action: str, previous_fingerprint: str) -> dict[str, Any]:
        """动作后的再观察：只报告"可观察变化"，不宣称业务成功。"""
        try:
            raw = await self.backend.observe()
        except BrowserError:
            return {
                "ok": True,
                "status": "executed",
                "outcome": "unknown",
                "hint": "动作已发出，但重新观察失败，无法确认页面变化。",
            }
        if not self.policy.allows_origin(raw.url):
            self._adopt_empty(raw)
            return {
                "ok": True,
                "status": "executed",
                "outcome": "unknown",
                "origin_allowed": False,
                "url": raw.url,
                "warning": "动作后落点来源不在允许范围内；后续动作已被阻止。",
            }
        adopted = self._adopt(raw)
        changed: bool | None
        if previous_fingerprint and raw.fingerprint:
            changed = raw.fingerprint != previous_fingerprint
            outcome = "changed" if changed else "no_observable_change"
        else:
            changed = None
            outcome = "unknown"
        return {
            "ok": True,
            "status": "executed",
            "action": action,
            "outcome": outcome,
            "changed": changed,
            "evidence": [self._page_evidence(raw)],
            **adopted,
        }

    @staticmethod
    def _page_evidence(raw: PageObservation) -> dict[str, Any]:
        """给任务验收用的页面证据（只含元数据，不含整页文本）。"""
        return {
            "kind": "page",
            "summary": f"{origin_of(raw.url)} {raw.title[:80]}".strip(),
            "ok": None,
            "detail": {
                "url": raw.url,
                "title": raw.title[:120],
                "fingerprint": raw.fingerprint,
            },
        }

    def _adopt(self, raw: PageObservation) -> dict[str, Any]:
        self._version += 1
        elements = tuple(raw.elements[: self.policy.max_elements])
        snapshot = Snapshot(version=self._version, observation=raw)
        self._snapshot = snapshot
        truncated = len(raw.text) > self.policy.max_text_chars
        public_elements = [
            element.to_public(f"s{self._version}:{index}")
            for index, element in enumerate(elements)
        ]
        return {
            "snapshot_id": f"s{self._version}",
            "page_version": self._version,
            "url": raw.url,
            "origin": origin_of(raw.url),
            "title": raw.title[:300],
            "text": raw.text[: self.policy.max_text_chars],
            "text_truncated": truncated,
            "elements": public_elements,
            "elements_truncated": len(raw.elements) > len(elements),
            "tabs": [tab.to_public() for tab in raw.tabs],
            "evidence": [self._page_evidence(raw)],
        }

    def _adopt_empty(self, raw: PageObservation) -> None:
        """记录一个"只保留元数据"的快照：不保留文本与元素，旧引用全部作废。"""
        self._version += 1
        self._snapshot = Snapshot(
            version=self._version,
            observation=PageObservation(
                url=raw.url, title=raw.title, tabs=raw.tabs, tab_id=raw.tab_id
            ),
        )

    async def _click(self, element: ElementInfo) -> None:
        await self.backend.click(element.backend_ref, frame_id=element.frame_id)

    async def _type(self, element: ElementInfo, text: str, *, submit: bool) -> None:
        await self.backend.type_text(
            element.backend_ref, text, frame_id=element.frame_id, submit=submit
        )

    async def _select(self, element: ElementInfo, value: str) -> None:
        await self.backend.select_option(
            element.backend_ref, value, frame_id=element.frame_id
        )


def _origin_gate(policy: BrowserPolicy) -> Any:
    """``browser_open`` 的准入闸：URL 来源不合规时在执行前拒绝。"""

    def gate(arguments: dict[str, Any], subject: Any = None) -> GateDecision:
        url = str(arguments.get("url", ""))
        if policy.allows_origin(url):
            return GateDecision()
        return GateDecision(reject=f"导航来源不在允许范围：{origin_of(url) or url!r}")

    return gate


def build_browser_tools(provider: BrowserProvider) -> list[ToolSpec]:
    """构造浏览器工具集；注册进 ToolRegistry 后走统一执行器与治理。"""
    policy = provider.policy
    rate_limit = policy.rate_limit()

    def _governance(action: str, *, sensitive: tuple[str, ...] = ()) -> ToolGovernance:
        return ToolGovernance(
            sensitive=sensitive,
            timeout_ms=policy.action_timeout_ms,
            rate_limit=rate_limit,
            approval=policy.approval_for(action),
            gate=_origin_gate(policy) if action == "open" else None,
        )

    return [
        ToolSpec(
            name=OBSERVE,
            description=(
                "观察当前浏览器页面：返回标题、URL、可见文本与带快照引用的可交互元素。"
                "元素引用（如 s3:2）只在最新快照内有效；页面变化后必须重新观察。"
            ),
            input_schema={"type": "object", "properties": {}},
            handler=provider.observe,
            governance=_governance("observe"),
        ),
        ToolSpec(
            name=CLICK,
            description=(
                "点击最新快照中的一个元素（ref 形如 s3:2）。"
                "返回执行后的观察结果与 outcome。"
            ),
            input_schema={
                "type": "object",
                "properties": {"ref": {"type": "string", "description": "元素引用"}},
                "required": ["ref"],
            },
            handler=provider.click,
            governance=_governance("click"),
        ),
        ToolSpec(
            name=TYPE,
            description=(
                "向最新快照中的输入框填入文本（ref 形如 s3:2）；submit=true 时随后提交。"
                "密码等敏感字段默认拒绝自动填写。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "text": {"type": "string", "description": "要输入的文本"},
                    "submit": {"type": "boolean", "default": False},
                },
                "required": ["ref", "text"],
            },
            handler=provider.type_text,
            governance=_governance("type", sensitive=("text",)),
        ),
        ToolSpec(
            name=SELECT,
            description="选择最新快照中的一个下拉项（ref + value）。",
            input_schema={
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["ref", "value"],
            },
            handler=provider.select,
            governance=_governance("select"),
        ),
        ToolSpec(
            name=TABS,
            description="列出当前浏览器的标签页（tab_id / url / title / 是否活动）。",
            input_schema={"type": "object", "properties": {}},
            handler=provider.list_tabs,
            governance=_governance("tabs"),
        ),
        ToolSpec(
            name=SWITCH_TAB,
            description="切换到指定标签页并返回其页面观察结果。",
            input_schema={
                "type": "object",
                "properties": {"tab_id": {"type": "string"}},
                "required": ["tab_id"],
            },
            handler=provider.switch_tab,
            governance=_governance("switch_tab"),
        ),
        ToolSpec(
            name=WAIT,
            description=(
                "等待页面条件成立（url_contains / text_contains 至少一个），"
                "超时返回 matched=false；结束后自动重新观察并刷新快照。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url_contains": {"type": "string"},
                    "text_contains": {"type": "string"},
                    "timeout_ms": {"type": "integer", "minimum": 1, "default": 10000},
                },
            },
            handler=provider.wait,
            governance=_governance("wait"),
        ),
        ToolSpec(
            name=OPEN,
            description="在当前标签页导航到目标 URL（需通过宿主声明的来源白名单）。",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            handler=provider.open,
            governance=_governance("open"),
        ),
    ]
