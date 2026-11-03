"""可选 pywinauto 后端：真实 Windows UIA 观察与操作。

状态：**已实现 · 本机待真机验收**（真机脚本：``scripts/windows_uia_smoke.py``）。
非 Windows 平台构造即报错，不静默降级。

边界（诚实声明）：

- 只支持 UIA 能表达的语义操作（Invoke / Toggle / SelectionItem / ValuePattern）；
  没有对应 Pattern 的控件不会被"猜一个坐标点下去"——``click`` 是显式兜底且默认关闭；
- 不越过 UAC / 安全桌面（提权窗口不在普通 UIA 会话内）；
- 控件句柄用 UIA ``runtime_id``；树变化后引用解析失败即按过期处理；
- 截图需要 Pillow（``[office]``/``[windows]`` 可能未装），缺失时返回 ``None``，
  不假装支持；
- 所有 UIA / COM 调用都是阻塞的，统一放到 ``asyncio.to_thread`` 执行，不堵塞事件循环。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import sys
from pathlib import Path
from typing import Any

from yai_core.integrations.desktop.models import (
    ControlInfo,
    DesktopObservation,
    WindowInfo,
)
from yai_core.integrations.desktop.policy import (
    DesktopBackendError,
    StaleControlRefError,
)


def _process_image(process_id: str) -> tuple[str, str]:
    """返回 ``(映像名, 完整路径)``；取不到时为空串，不阻塞观察。"""
    if sys.platform != "win32" or not process_id.isdigit():
        return "", ""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, int(process_id))
        if not handle:
            return "", ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return "", ""
            path = buffer.value
            return Path(path).name, path
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 进程已退出/权限不足时降级为空
        return "", ""


def _runtime_key(element_info: Any) -> str:
    runtime_id = getattr(element_info, "runtime_id", None)
    if runtime_id is None:
        return ""
    if isinstance(runtime_id, (list, tuple)):
        return ".".join(str(part) for part in runtime_id)
    return str(runtime_id)


class PywinautoDesktopBackend:
    """用 pywinauto（UIA backend）驱动真实 Windows 桌面。"""

    def __init__(self, *, timeout_ms: int = 10_000) -> None:
        if sys.platform != "win32":
            raise DesktopBackendError("Windows Desktop Provider 只能在 Windows 上使用")
        self.timeout_ms = timeout_ms
        self._desktop: Any = None

    def _require_desktop(self) -> Any:
        if self._desktop is None:
            try:
                from pywinauto import Desktop
            except ImportError as exc:  # pragma: no cover - 仅在缺可选依赖时触发
                raise DesktopBackendError(
                    "未安装 pywinauto；请 pip install 'yai-agent-core[windows]'"
                ) from exc
            self._desktop = Desktop(backend="uia")
        return self._desktop

    # ---------- 观察 ----------

    async def list_windows(self) -> list[WindowInfo]:
        return await asyncio.to_thread(self._list_windows_sync)

    def _list_windows_sync(self) -> list[WindowInfo]:
        desktop = self._require_desktop()
        out: list[WindowInfo] = []
        for window in desktop.windows():
            info = window.element_info
            try:
                if not info.visible:
                    continue
            except Exception:  # noqa: BLE001 - 个别窗口属性取不到不应中断枚举
                pass
            pid = str(info.process_id)
            process_name, process_path = _process_image(pid)
            out.append(
                WindowInfo(
                    window_id=str(window.handle),
                    title=window.window_text(),
                    process_id=pid,
                    process_name=process_name,
                    process_path=process_path,
                    class_name=str(info.class_name or ""),
                    enabled=bool(getattr(info, "enabled", True)),
                    visible=bool(getattr(info, "visible", True)),
                )
            )
        return out

    async def observe(self, window_id: str) -> DesktopObservation:
        return await asyncio.to_thread(self._observe_sync, window_id)

    def _observe_sync(self, window_id: str) -> DesktopObservation:
        desktop = self._require_desktop()
        try:
            window = desktop.window(handle=int(window_id))
            info = window.element_info
            controls = window.descendants()
        except Exception as exc:  # noqa: BLE001 - 窗口关闭 / 句柄失效
            raise DesktopBackendError(f"观察窗口失败：{type(exc).__name__}: {exc}") from exc
        collected: list[ControlInfo] = []
        for control in controls:
            control_info = control.element_info
            key = _runtime_key(control_info)
            if not key:
                continue
            name = ""
            try:
                name = control.window_text()
            except Exception:  # noqa: BLE001
                name = str(getattr(control_info, "name", "") or "")
            value = ""
            # Edit 用 ValuePattern；ComboBox 没有 get_value 时用 selected_text
            # （只读展示当前选择，不改变控件）。
            for getter_name in ("get_value", "selected_text"):
                getter = getattr(control, getter_name, None)
                if getter is None:
                    continue
                try:
                    candidate = str(getter() or "")
                except Exception:  # noqa: BLE001 - 该 Pattern 不适用
                    continue
                if candidate:
                    value = candidate
                    break
            sensitive = False
            try:
                sensitive = bool(control.is_password())
            except Exception:  # noqa: BLE001
                sensitive = False
            collected.append(
                ControlInfo(
                    control_id=key,
                    role=str(getattr(control_info, "control_type", "") or ""),
                    name=name,
                    automation_id=str(getattr(control_info, "automation_id", "") or ""),
                    enabled=bool(getattr(control_info, "enabled", True)),
                    value=value,
                    sensitive=sensitive,
                )
            )
        focused = ""
        try:
            focused_element = self._focused_runtime_key(window)
            focused = focused_element
        except Exception:  # noqa: BLE001
            focused = ""
        fingerprint = _observation_fingerprint(window_id, window, collected, focused)
        pid = str(info.process_id)
        process_name, process_path = _process_image(pid)
        return DesktopObservation(
            window=WindowInfo(
                window_id=window_id,
                title=window.window_text(),
                process_id=pid,
                process_name=process_name,
                process_path=process_path,
                class_name=str(info.class_name or ""),
                enabled=bool(getattr(info, "enabled", True)),
                visible=bool(getattr(info, "visible", True)),
            ),
            controls=tuple(collected),
            focused=focused,
            fingerprint=fingerprint,
        )

    def _focused_runtime_key(self, window: Any) -> str:
        try:
            element = window.element_info
            focused = element.focused_element
            return _runtime_key(focused)
        except Exception:  # noqa: BLE001 - 部分后端不暴露 focused_element
            return ""

    # ---------- 动作 ----------

    def _find_control(self, window: Any, control_id: str) -> Any:
        for control in window.descendants():
            if _runtime_key(control.element_info) == control_id:
                return control
        raise StaleControlRefError(
            "控件已不在窗口树中（可能已重新布局）；请重新观察后再操作"
        )

    async def focus(self, window_id: str) -> None:
        await asyncio.to_thread(self._focus_sync, window_id)

    def _focus_sync(self, window_id: str) -> None:
        desktop = self._require_desktop()
        try:
            window = desktop.window(handle=int(window_id))
            window.set_focus()
        except Exception as exc:  # noqa: BLE001
            raise DesktopBackendError(f"聚焦窗口失败：{type(exc).__name__}: {exc}") from exc

    async def is_foreground(self, window_id: str) -> bool:
        return await asyncio.to_thread(self._is_foreground_sync, window_id)

    def _is_foreground_sync(self, window_id: str) -> bool:
        desktop = self._require_desktop()
        try:
            active = desktop.get_active()
            return active is not None and str(active.handle) == str(window_id)
        except Exception:  # noqa: BLE001 - 拿不到活动窗口时保守返回 False
            return False

    async def invoke(self, window_id: str, control_id: str) -> None:
        await asyncio.to_thread(self._invoke_sync, window_id, control_id)

    def _invoke_sync(self, window_id: str, control_id: str) -> None:
        desktop = self._require_desktop()
        window = desktop.window(handle=int(window_id))
        control = self._find_control(window, control_id)
        for method in ("invoke", "toggle", "select"):
            action = getattr(control, method, None)
            if action is None:
                continue
            try:
                action()
                return
            except Exception:  # noqa: BLE001 - 该 Pattern 不适用就换下一个
                continue
        raise DesktopBackendError(
            "控件没有可用的 UIA 语义操作（Invoke/Toggle/Select）；已拒绝坐标猜测"
        )

    async def set_value(self, window_id: str, control_id: str, value: str) -> None:
        await asyncio.to_thread(self._set_value_sync, window_id, control_id, value)

    def _set_value_sync(self, window_id: str, control_id: str, value: str) -> None:
        desktop = self._require_desktop()
        window = desktop.window(handle=int(window_id))
        control = self._find_control(window, control_id)
        try:
            control.set_edit_text(value)
        except Exception as exc:  # noqa: BLE001
            raise DesktopBackendError(
                f"控件不支持 ValuePattern 设置值：{type(exc).__name__}: {exc}"
            ) from exc

    async def select_option(self, window_id: str, control_id: str, value: str) -> None:
        await asyncio.to_thread(self._select_sync, window_id, control_id, value)

    def _select_sync(self, window_id: str, control_id: str, value: str) -> None:
        desktop = self._require_desktop()
        window = desktop.window(handle=int(window_id))
        control = self._find_control(window, control_id)
        for method in ("select", "set_edit_text"):
            action = getattr(control, method, None)
            if action is None:
                continue
            try:
                action(value)
                return
            except Exception:  # noqa: BLE001 - Pattern 不适用就换下一个
                continue
        raise DesktopBackendError(
            "控件不支持下拉选择（无 ComboBox.select / ValuePattern）；已拒绝坐标猜测"
        )

    async def click(self, window_id: str, control_id: str) -> None:
        await asyncio.to_thread(self._click_sync, window_id, control_id)

    def _click_sync(self, window_id: str, control_id: str) -> None:
        desktop = self._require_desktop()
        window = desktop.window(handle=int(window_id))
        control = self._find_control(window, control_id)
        try:
            control.click_input()
        except Exception as exc:  # noqa: BLE001
            raise DesktopBackendError(f"坐标点击失败：{type(exc).__name__}: {exc}") from exc

    async def screenshot(self, window_id: str) -> bytes | None:
        return await asyncio.to_thread(self._screenshot_sync, window_id)

    def _screenshot_sync(self, window_id: str) -> bytes | None:
        desktop = self._require_desktop()
        try:
            from PIL import ImageGrab
        except ImportError:
            return None
        try:
            window = desktop.window(handle=int(window_id))
            rect = window.rectangle()
            image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        except Exception:  # noqa: BLE001 - 截图是增强，失败返回 None
            return None


def _observation_fingerprint(
    window_id: str,
    window: Any,
    controls: list[ControlInfo],
    focused: str,
) -> str:
    try:
        title = window.window_text()
    except Exception:  # noqa: BLE001
        title = ""
    digest = hashlib.sha1()  # noqa: S324 - 非安全用途，仅做状态摘要
    digest.update(f"{window_id}\x00{title}\x00{focused}".encode("utf-8", "replace"))
    for control in controls:
        digest.update(
            f"\x00{control.control_id}:{control.name}:{control.value}:{control.enabled}".encode(
                "utf-8", "replace"
            )
        )
    return digest.hexdigest()
