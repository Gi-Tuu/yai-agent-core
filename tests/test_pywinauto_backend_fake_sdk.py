"""PywinautoDesktopBackend 的离线回归：用假 pywinauto SDK 覆盖真实后端代码路径。

不启动真实窗口；仅 Windows 构造器允许，其他平台跳过。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from yai_core.integrations.desktop.policy import (
    DesktopBackendError,
    StaleControlRefError,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="PywinautoDesktopBackend 只能在 Windows 构造"
)

if sys.platform == "win32":
    from yai_core.integrations.desktop.pywinauto_backend import (
        PywinautoDesktopBackend,
    )


class FakeElementInfo:
    def __init__(
        self,
        *,
        runtime_id=(1, 1),
        process_id=4242,
        class_name="FakeClass",
        enabled=True,
        visible=True,
        control_type="Button",
        automation_id="ok",
        name="确定",
        focused_element=None,
    ) -> None:
        self.runtime_id = list(runtime_id) if runtime_id is not None else None
        self.process_id = process_id
        self.class_name = class_name
        self.enabled = enabled
        self.visible = visible
        self.control_type = control_type
        self.automation_id = automation_id
        self.name = name
        self.focused_element = focused_element


class FakeControl:
    def __init__(self, *, runtime_id=(1, 2), name="确定", value="", password=False):
        self.element_info = FakeElementInfo(
            runtime_id=runtime_id,
            control_type="Button",
            name=name,
        )
        self._name = name
        self._value = value
        self._password = password
        self.invoked = 0
        self.toggled = 0
        self.clicked = 0
        self.text_set = ""

    def window_text(self) -> str:
        return self._name

    def get_value(self) -> str:
        return self._value

    def is_password(self) -> bool:
        return self._password

    def invoke(self) -> None:
        self.invoked += 1

    def toggle(self) -> None:
        self.toggled += 1

    def select(self) -> None:
        raise RuntimeError("not supported")

    def set_edit_text(self, value: str) -> None:
        self.text_set = value

    def click_input(self) -> None:
        self.clicked += 1


class FakeRect:
    left, top, right, bottom = 0, 0, 10, 10


class FakeWindow:
    def __init__(self, handle: str, title: str, controls: list[FakeControl]) -> None:
        self.handle = int(handle)
        self._title = title
        self._controls = controls
        self.focused = 0
        self.rect = FakeRect()
        self.element_info = FakeElementInfo(
            runtime_id=None,
            control_type="Window",
            name=title,
            focused_element=FakeElementInfo(runtime_id=(1, 2), name="focused"),
        )

    def window_text(self) -> str:
        return self._title

    def descendants(self) -> list[FakeControl]:
        return list(self._controls)

    def set_focus(self) -> None:
        self.focused += 1

    def rectangle(self) -> FakeRect:
        return self.rect


class FakeDesktop:
    def __init__(self) -> None:
        self.controls = [FakeControl(), FakeControl(runtime_id=(1, 3), name="姓名")]
        self.window1 = FakeWindow("1001", "测试窗口", self.controls)
        self.window2 = FakeWindow("1002", "隐藏窗口", [])

    def windows(self) -> list[FakeWindow]:
        return [self.window1, self.window2]

    def window(self, *, handle: int) -> FakeWindow:
        if handle == 1001:
            return self.window1
        if handle == 1002:
            return self.window2
        raise RuntimeError("窗口不存在")

    def get_active(self) -> FakeWindow:
        return self.window1


@pytest.fixture()
def fake_pywinauto(monkeypatch: pytest.MonkeyPatch) -> FakeDesktop:
    desktop = FakeDesktop()
    module = types.ModuleType("pywinauto")
    module.Desktop = lambda backend="uia": desktop  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pywinauto", module)
    return desktop


def test_backend_list_windows_and_observe(fake_pywinauto: FakeDesktop) -> None:
    backend = PywinautoDesktopBackend()
    windows = asyncio.run(backend.list_windows())
    assert [window.title for window in windows] == ["测试窗口", "隐藏窗口"]

    observation = asyncio.run(backend.observe("1001"))
    assert observation.window.title == "测试窗口"
    assert observation.window.process_id == "4242"
    assert [control.name for control in observation.controls] == ["确定", "姓名"]
    assert observation.focused
    assert observation.fingerprint


def test_backend_actions_and_stale_control(fake_pywinauto: FakeDesktop) -> None:
    backend = PywinautoDesktopBackend()
    control = fake_pywinauto.controls[0]
    asyncio.run(backend.invoke("1001", "1.2"))
    assert control.invoked == 1
    asyncio.run(backend.set_value("1001", "1.2", "hello"))
    assert control.text_set == "hello"
    asyncio.run(backend.click("1001", "1.2"))
    assert control.clicked == 1
    asyncio.run(backend.focus("1001"))
    assert fake_pywinauto.window1.focused == 1
    assert asyncio.run(backend.is_foreground("1001")) is True

    with pytest.raises(StaleControlRefError):
        asyncio.run(backend.invoke("1001", "9.9"))
    with pytest.raises(DesktopBackendError):
        asyncio.run(backend.observe("9999"))
