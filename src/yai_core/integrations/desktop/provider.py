"""Desktop Provider：把 Windows UIA 的"观察 → 引用 → 操作 → 再观察"做成受治理工具。

与 Browser Provider 同一套模式（v0.26 任务书 §6）：

- 控件引用带快照版本（``c<N>`` / 引用形如 ``d3:2``），窗口状态变化后旧引用作废；
- 动作只返回 ``executed + outcome``，不宣称任务完成（任务级结论交给验收契约）；
- 工具注册进 ``ToolRegistry``、由 ``ToolExecutor`` 统一治理，不另起执行通道；
- 高风险控件名默认拒绝、几何点击默认关闭、焦点不在目标窗口时拒绝物理输入；
- ``pause()`` 支持用户接管：暂停后所有动作被拒绝，直到 ``resume()``；
- 不越过 UAC / 安全桌面——后端直接报错，Provider 不改写为成功。
"""

from __future__ import annotations

import warnings
from typing import Any

from yai_core.diagnostics import DiagnosticSink
from yai_core.integrations.desktop.backend import DesktopBackend
from yai_core.integrations.desktop.models import (
    ControlInfo,
    DesktopObservation,
    DesktopSnapshot,
)
from yai_core.integrations.desktop.policy import (
    DesktopError,
    DesktopPolicy,
    DesktopSecurityWarning,
    StaleControlRefError,
)
from yai_core.types import ToolGovernance, ToolSpec

LIST_WINDOWS = "desktop_list_windows"
OBSERVE = "desktop_observe"
INVOKE = "desktop_invoke"
SET_VALUE = "desktop_set_value"
FOCUS = "desktop_focus"
CLICK = "desktop_click"
PAUSE = "desktop_pause"
RESUME = "desktop_resume"


class DesktopProvider:
    """受治理的 Windows 桌面工具集合；一个实例对应一个受控桌面会话。"""

    def __init__(
        self,
        backend: DesktopBackend,
        *,
        policy: DesktopPolicy | None = None,
        on_diagnostic: DiagnosticSink | None = None,
    ) -> None:
        if policy is not None and not isinstance(policy, DesktopPolicy):
            raise ValueError("policy 必须是 DesktopPolicy 实例或 None")
        if policy is None:
            warnings.warn(
                "DesktopProvider 未显式传入 DesktopPolicy，使用开发/测试宽松默认"
                "（不限制进程/窗口）；生产请用 DesktopPolicy.secure([...]) 声明可操作范围。",
                DesktopSecurityWarning,
                stacklevel=2,
            )
            policy = DesktopPolicy.development()
        self.backend = backend
        self.policy = policy
        self.on_diagnostic = on_diagnostic
        self._version = 0
        self._snapshot: DesktopSnapshot | None = None
        self._paused = False
        self._pause_reason = ""
        self.last_action: dict[str, Any] | None = None

    # ---------- 用户接管 ----------

    def pause(self, reason: str = "用户接管") -> None:
        """暂停桌面动作（用户接管）；之后所有操作返回 ``paused``。"""
        self._paused = True
        self._pause_reason = reason

    def resume(self) -> None:
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def snapshot_id(self) -> str | None:
        return self._snapshot.snapshot_id if self._snapshot else None

    # ---------- 观察 ----------

    async def list_windows(self) -> dict[str, Any]:
        try:
            windows = await self.backend.list_windows()
        except DesktopError as exc:
            return {"ok": False, "status": "backend_error", "error": str(exc)}
        # 元数据读取也受进程/标题白名单约束：被禁窗口的标题/进程不得进入模型上下文。
        visible = [
            window
            for window in windows
            if self.policy.allows_window(
                window.window_id, window.title, window.process_id
            )
        ]
        return {
            "ok": True,
            "status": "observed",
            "windows": [{**window.to_public(), "allowed": True} for window in visible],
            "hidden_count": len(windows) - len(visible),
        }

    async def observe(self, window_id: str) -> dict[str, Any]:
        # 观察是只读操作：用户接管期间仍允许查看，方便其判断当前状态。
        try:
            observation = await self.backend.observe(window_id)
        except DesktopError as exc:
            return {"ok": False, "status": "backend_error", "error": str(exc)}
        if not isinstance(observation, DesktopObservation):
            return {
                "ok": False,
                "status": "backend_error",
                "error": "backend.observe() 必须返回 DesktopObservation",
            }
        window = observation.window
        if not self.policy.allows_window(window.window_id, window.title, window.process_id):
            self._adopt_empty(observation)
            return {
                "ok": False,
                "status": "window_not_allowed",
                "window_id": window.window_id,
                "title": window.title,
                "hint": "该窗口进程 / 标题不在宿主允许范围内；不要继续读取或操作它。",
            }
        return {"ok": True, "status": "observed", **self._adopt(observation)}

    # ---------- 动作 ----------

    async def focus(self, window_id: str) -> dict[str, Any]:
        refusal = self._check_paused()
        if refusal is not None:
            return refusal
        windows = await self.list_windows()
        target = next(
            (item for item in windows.get("windows", []) if item["window_id"] == window_id),
            None,
        )
        if target is None:
            return {"ok": False, "status": "window_not_found", "window_id": window_id}
        if not target.get("allowed"):
            return {"ok": False, "status": "window_not_allowed", "window_id": window_id}
        try:
            await self.backend.focus(window_id)
        except DesktopError as exc:
            return {"ok": False, "status": "backend_error", "error": str(exc)}
        return {"ok": True, "status": "executed", "outcome": "focused", "window_id": window_id}

    async def invoke(self, ref: str) -> dict[str, Any]:
        return await self._act("invoke", ref, self._invoke_control)

    async def set_value(self, ref: str, value: str) -> dict[str, Any]:
        return await self._act(
            "set_value",
            ref,
            lambda window, control: self._set(window, control, value),
        )

    async def click(self, ref: str) -> dict[str, Any]:
        if not self.policy.allow_geometry_click:
            return {
                "ok": False,
                "status": "geometry_click_disabled",
                "hint": "宿主未开启坐标兜底；请优先使用语义 UIA 操作（invoke / set_value）。",
            }
        return await self._act("click", ref, self._click_control, require_foreground=True)

    # ---------- 内部 ----------

    def _check_paused(self) -> dict[str, Any] | None:
        if not self._paused:
            return None
        return {
            "ok": False,
            "status": "paused",
            "hint": "桌面操作已暂停（用户接管）；恢复后才能继续（只读观察仍可用）。",
        }

    def _resolve(self, ref: str) -> tuple[str, ControlInfo]:
        if self._snapshot is None:
            raise StaleControlRefError("尚未观察窗口；请先调用 desktop_observe")
        version, _, index_part = (ref or "").partition(":")
        expected = f"d{self._snapshot.version}"
        if version != expected:
            raise StaleControlRefError(
                f"控件引用 {ref!r} 来自旧快照；最新快照是 {expected}，请重新观察"
            )
        try:
            index = int(index_part)
        except ValueError as exc:
            raise StaleControlRefError(f"控件引用格式非法：{ref!r}") from exc
        control = self._snapshot.observation.control_by_index(index)
        if control is None:
            raise StaleControlRefError(f"控件引用 {ref!r} 不存在于最新快照")
        return self._snapshot.window_id, control

    async def _act(
        self,
        action: str,
        ref: str,
        call: Any,
        *,
        require_foreground: bool = False,
    ) -> dict[str, Any]:
        refusal = self._check_paused()
        if refusal is not None:
            return refusal
        try:
            window_id, control = self._resolve(ref)
        except StaleControlRefError as exc:
            return {
                "ok": False,
                "status": "stale_ref",
                "error": str(exc),
                "snapshot_id": self.snapshot_id,
                "hint": "请重新调用 desktop_observe 获取新引用后再操作。",
            }
        assert self._snapshot is not None
        window = self._snapshot.observation.window
        if not self.policy.allows_window(window.window_id, window.title, window.process_id):
            return {"ok": False, "status": "window_not_allowed", "window_id": window.window_id}
        if not control.enabled:
            return {"ok": False, "status": "control_disabled", "ref": ref}
        if self.policy.is_high_risk_name(control.name) and not self.policy.allow_high_risk_controls:
            return {
                "ok": False,
                "status": "high_risk_blocked",
                "ref": ref,
                "name": control.name,
                "hint": "该控件命中高风险关键词；宿主未授权自动操作。",
            }
        if action == "set_value" and control.sensitive:
            return {
                "ok": False,
                "status": "sensitive_control_blocked",
                "ref": ref,
                "hint": "敏感控件（如密码框）默认拒绝自动填写。",
            }
        if require_foreground:
            try:
                foreground = await self.backend.is_foreground(window_id)
            except DesktopError as exc:
                return {"ok": False, "status": "backend_error", "error": str(exc)}
            if not foreground:
                return {
                    "ok": False,
                    "status": "not_foreground",
                    "window_id": window_id,
                    "hint": (
                        "目标窗口不在前台（可能被用户切换）；"
                        "请先 desktop_focus，或交给用户处理。"
                    ),
                }
        previous = self._snapshot.observation.fingerprint
        try:
            await call(window_id, control)
        except DesktopError as exc:
            result = {
                "ok": False,
                "status": "backend_error",
                "error": str(exc),
                "snapshot_id": self.snapshot_id,
            }
            self.last_action = {"action": action, **result}
            return result
        result = await self._settle(action, window_id, previous)
        self.last_action = result
        return result

    async def _settle(
        self, action: str, window_id: str, previous_fingerprint: str
    ) -> dict[str, Any]:
        try:
            observation = await self.backend.observe(window_id)
        except DesktopError:
            return {
                "ok": True,
                "status": "executed",
                "outcome": "unknown",
                "hint": "动作已发出，但重新观察失败，无法确认窗口状态。",
            }
        adopted = self._adopt(observation)
        if previous_fingerprint and observation.fingerprint:
            changed = observation.fingerprint != previous_fingerprint
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
            **adopted,
        }

    async def _invoke_control(self, window_id: str, control: ControlInfo) -> None:
        await self.backend.invoke(window_id, control.control_id)

    async def _set(self, window_id: str, control: ControlInfo, value: str) -> None:
        await self.backend.set_value(window_id, control.control_id, value)

    async def _click_control(self, window_id: str, control: ControlInfo) -> None:
        await self.backend.click(window_id, control.control_id)

    def _adopt(self, observation: DesktopObservation) -> dict[str, Any]:
        self._version += 1
        controls = tuple(observation.controls[: self.policy.max_controls])
        snapshot = DesktopSnapshot(
            version=self._version,
            observation=observation,
            window_id=observation.window.window_id,
        )
        self._snapshot = snapshot
        return {
            "snapshot_id": f"d{self._version}",
            "page_version": self._version,
            "window": observation.window.to_public(),
            "focused": observation.focused,
            "controls": [
                control.to_public(f"d{self._version}:{index}")
                for index, control in enumerate(controls)
            ],
            "controls_truncated": len(observation.controls) > len(controls),
            "evidence": [
                {
                    "kind": "desktop",
                    "summary": f"{observation.window.title[:80]}",
                    "ok": None,
                    "detail": {
                        "window_id": observation.window.window_id,
                        "fingerprint": observation.fingerprint,
                    },
                }
            ],
        }

    def _adopt_empty(self, observation: DesktopObservation) -> None:
        self._version += 1
        self._snapshot = DesktopSnapshot(
            version=self._version,
            observation=DesktopObservation(window=observation.window),
            window_id=observation.window.window_id,
        )


def _governance(
    policy: DesktopPolicy, action: str, *, sensitive: tuple[str, ...] = ()
) -> ToolGovernance:
    return ToolGovernance(
        sensitive=sensitive,
        timeout_ms=policy.action_timeout_ms,
        rate_limit=policy.rate_limit(),
        approval=policy.approval_for(action),
    )


def build_desktop_tools(provider: DesktopProvider) -> list[ToolSpec]:
    """构造桌面工具集；注册进 ToolRegistry 后走统一执行器与治理。"""
    policy = provider.policy
    return [
        ToolSpec(
            name=LIST_WINDOWS,
            description="列出当前桌面的顶层窗口（标题 / 进程 / 是否在宿主允许范围内）。",
            input_schema={"type": "object", "properties": {}},
            handler=provider.list_windows,
            governance=_governance(policy, "list_windows"),
        ),
        ToolSpec(
            name=OBSERVE,
            description=(
                "观察指定窗口的 UIA 控件树：返回控件快照与引用（如 d3:2）。"
                "引用只在最新快照内有效，窗口变化后必须重新观察。"
            ),
            input_schema={
                "type": "object",
                "properties": {"window_id": {"type": "string"}},
                "required": ["window_id"],
            },
            handler=provider.observe,
            governance=_governance(policy, "observe"),
        ),
        ToolSpec(
            name=INVOKE,
            description=(
                "通过 UIA 语义 Pattern 操作控件（按钮 / 复选项 / 列表项等），"
                "参数是最新快照里的引用（如 d3:2）。高风险控件默认拒绝。"
            ),
            input_schema={
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
            handler=provider.invoke,
            governance=_governance(policy, "invoke"),
        ),
        ToolSpec(
            name=SET_VALUE,
            description=(
                "通过 UIA ValuePattern 设置输入框的值（引用形如 d3:2）；"
                "敏感控件默认拒绝，优先于模拟键盘输入。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["ref", "value"],
            },
            handler=provider.set_value,
            governance=_governance(policy, "set_value", sensitive=("value",)),
        ),
        ToolSpec(
            name=FOCUS,
            description="把指定窗口带到前台（显式动作）；物理点击前需要目标窗口在前台。",
            input_schema={
                "type": "object",
                "properties": {"window_id": {"type": "string"}},
                "required": ["window_id"],
            },
            handler=provider.focus,
            governance=_governance(policy, "focus"),
        ),
        ToolSpec(
            name=CLICK,
            description=(
                "坐标兜底点击控件中心（仅当宿主显式开启 geometry click）；"
                "默认关闭，优先使用 invoke / set_value。"
            ),
            input_schema={
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
            handler=provider.click,
            governance=_governance(policy, "click"),
        ),
        ToolSpec(
            name=PAUSE,
            description="暂停桌面操作（用户接管）；恢复前所有桌面动作都会被拒绝。",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: _pause(provider),
            governance=_governance(policy, "pause"),
        ),
        ToolSpec(
            name=RESUME,
            description="恢复桌面操作（用户交还控制权）。",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: _resume(provider),
            governance=_governance(policy, "resume"),
        ),
    ]


def _pause(provider: DesktopProvider) -> dict[str, Any]:
    provider.pause()
    return {"ok": True, "status": "paused"}


def _resume(provider: DesktopProvider) -> dict[str, Any]:
    provider.resume()
    return {"ok": True, "status": "resumed"}
