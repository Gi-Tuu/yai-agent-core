"""单个专员的运行时：一个专员 = 一个应用内置 Core 的界面侧替身。

与工作台（:mod:`shell.desktop.workbench`）的分工：

- **不拥有线程与事件循环**：loop 由工作台共享注入，避免每专员一条线程；
- **状态完全独立**：busy / 挂起决策 / Core 开关 / 权限挡位 / 未读，互不串味；
- 事件与状态只经 Qt 信号往外发，QML 不直接连专员，一律经工作台转发（带 sid）。

线程纪律：下行事件跨线程 emit（Qt 自动按 QueuedConnection 投递到 GUI 线程），
上行决策经 ``call_soon_threadsafe`` 送回内核线程。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import Property, QObject, Signal, Slot

#: 权限挡位：全部审批 / 部分审批（读放行、写询问）/ 无需审批。
PERMISSION_MODES = ("manual", "partial", "auto")

#: 终结型事件：见到它们本次任务即结束。
_TERMINAL_EVENTS = ("done", "error", "cancelled")

#: 事件流工厂：(原始任务, 通道) -> 事件异步迭代器。
StreamFactory = Callable[[str, Any], AsyncIterator[Any]]


@dataclass(frozen=True)
class SpecialistSpec:
    """专员装配描述：显示身份 + 两种形态的事件流工厂。"""

    id: str
    name: str
    glyph: str
    demo_factory: StreamFactory
    live_factory: StreamFactory | None = None
    #: 该专员代码工具的持久化仓库路径；None = 没有可管理的工具仓库。
    code_storage: Path | None = None
    #: 该专员路由自学习状态的持久化路径；None = 不启用/不展示学习面板。
    learning_path: Path | None = None


def event_name(event: Any) -> str:
    """取事件类型字符串（兼容 StrEnum 与普通字符串）。"""
    event_type = getattr(event, "type", None)
    return event_type.value if hasattr(event_type, "value") else str(event_type)


def dump(data: Any) -> str:
    """把事件负载序列化为 JSON 字符串（QML 侧 JSON.parse 后渲染）。"""
    return json.dumps(data or {}, ensure_ascii=False, default=str)


class SpecialistRuntime(QObject):
    """一个专员的会话中枢：任务生命周期 + 挂起决策 + 独立权限挡位。"""

    stateChanged = Signal()
    #: (事件类型, JSON 负载) —— 界面动效的唯一数据源。
    eventReceived = Signal(str, str)
    #: 一次性提示（启动被拒、错误等），不进事件流。
    noticeRaised = Signal(str)
    #: 本专员状态变化需要工作台刷新列表行。
    unreadChanged = Signal(int)

    def __init__(
        self,
        spec: SpecialistSpec,
        loop: asyncio.AbstractEventLoop,
        *,
        factory: StreamFactory | None = None,
        enabled: bool = True,
        permission_mode: str = "partial",
        run_mode: str = "live",
    ) -> None:
        super().__init__()
        if permission_mode not in PERMISSION_MODES:
            raise ValueError(
                f"未知权限挡位 {permission_mode!r}，可选 {PERMISSION_MODES}"
            )
        self.spec = spec
        self._loop = loop
        self._factory = factory or spec.demo_factory
        self._enabled = enabled
        self._permission_mode = permission_mode
        self._run_mode = run_mode
        self._busy = False
        self._unread = 0
        self._cancel_requested = False
        self._pending: tuple[str, asyncio.Future] | None = None
        self._pending_kind = ""
        self._pending_json = "{}"
        self._task: asyncio.Task | None = None
        self._last_task = ""
        self._can_retry = False
        self._lock = threading.Lock()

    # ---------- 供工作台读取的身份与状态 ----------

    @property
    def sid(self) -> str:
        return self.spec.id

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def pending_kind(self) -> str:
        return self._pending_kind

    @property
    def pending_payload(self) -> str:
        return self._pending_json

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    @property
    def run_mode(self) -> str:
        return self._run_mode

    @property
    def unread(self) -> int:
        return self._unread

    @property
    def can_retry(self) -> bool:
        return self._can_retry

    def clear_unread(self) -> None:
        if self._unread:
            self._unread = 0
            self.unreadChanged.emit(0)
            self.stateChanged.emit()

    # ---------- QML 可见状态（经工作台聚合） ----------

    def _get_busy(self) -> bool:
        return self._busy

    def _get_unread(self) -> int:
        return self._unread

    def _get_core_enabled(self) -> bool:
        return self._enabled

    def _get_permission_mode(self) -> str:
        return self._permission_mode

    busyProp = Property(bool, _get_busy, notify=stateChanged)
    unreadProp = Property(int, _get_unread, notify=stateChanged)
    coreEnabled = Property(bool, _get_core_enabled, notify=stateChanged)
    permissionMode = Property(str, _get_permission_mode, notify=stateChanged)

    def _get_can_retry(self) -> bool:
        return self._can_retry

    canRetry = Property(bool, _get_can_retry, notify=stateChanged)

    # ---------- 任务生命周期 ----------

    @Slot(str, result=bool)
    def startTask(self, text: str) -> bool:
        """启动一次任务；被拒时给出界面可见的原因。"""
        task_text = (text or "").strip()
        with self._lock:
            if not self._enabled:
                self.noticeRaised.emit(f"{self.spec.name}已停用，先启用该专员")
                return False
            if self._busy:
                self.noticeRaised.emit(f"{self.spec.name}正在执行任务，可先终止")
                return False
            if not task_text:
                self.noticeRaised.emit("任务不能为空")
                return False
            self._busy = True
            self._cancel_requested = False
            self._last_task = task_text
            self._can_retry = False
        self.stateChanged.emit()
        asyncio.run_coroutine_threadsafe(self._guarded(task_text), self._loop)
        return True

    @Slot(result=bool)
    def retry(self) -> bool:
        """出错后用上次的任务文本重跑一次；busy 或没有历史时不动作。"""
        if self._busy or not self._last_task:
            return False
        return self.startTask(self._last_task)

    @Slot()
    def cancel(self) -> None:
        """终止当前任务：先兑现挂起决策，再取消后台协程。

        任务可能还没在事件循环里起跑，所以同时置取消标记，由运行体入场时收尾。
        """
        task, pending = self._task, self._pending
        self._cancel_requested = True

        def _do_cancel() -> None:
            if pending is not None:
                kind, future = pending
                if not future.done():
                    future.set_result(False if kind == "confirm" else "")
            if task is not None:
                task.cancel()

        self._loop.call_soon_threadsafe(_do_cancel)

    async def _guarded(self, task_text: str) -> None:
        from shell.desktop.channel import QtChannel

        self._task = asyncio.current_task()
        if self._cancel_requested:
            self._finish("cancelled", {"reason": "任务已被用户终止"})
            return
        stream = self._factory(task_text, QtChannel(self))
        ended = False
        try:
            async for event in stream:
                name = event_name(event)
                self.eventReceived.emit(name, dump(getattr(event, "data", {})))
                if name in _TERMINAL_EVENTS:
                    ended = True
                    if name == "error":
                        self._can_retry = True
                    if name == "done":
                        break
        except asyncio.CancelledError:
            ended = True
            self._finish("cancelled", {"reason": "任务已被用户终止"})
            raise
        except Exception as exc:  # noqa: BLE001 - 界面需看到错误而非静止不动
            ended = True
            self._can_retry = True
            self._finish("error", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            await stream.aclose()
            if not ended:
                # 流自然结束但未显式给 done：补一个终结事件，界面才不会卡在"执行中"。
                self.eventReceived.emit("done", "{}")
            self._settle()

    def _finish(self, name: str, data: dict) -> None:
        """投递终结事件并收尾状态。"""
        self.eventReceived.emit(name, dump(data))
        self._settle()

    def _settle(self) -> None:
        """清挂起、复位忙标记（幂等：重复调用不再发状态）。"""
        self.close_prompt()
        self._task = None
        with self._lock:
            if not self._busy:
                return
            self._busy = False
        self.stateChanged.emit()

    # ---------- 授权 / 澄清挂起决策 ----------

    def open_prompt(self, kind: str, future: asyncio.Future, data: dict) -> None:
        """由 QtChannel 调用（内核线程）：登记挂起决策，等待界面兑现。

        clarify_requested / permission_asked 已由内核在事件流里先 yield，并经
        ``_guarded`` 的统一转发投递给界面（每个事件严格一次）；这里只登记
        Future 与卡片负载，不再重复 emit，否则同一条提问会显示两遍。
        """
        self._pending = (kind, future)
        self._pending_kind = kind
        self._pending_json = dump(data)
        self.stateChanged.emit()

    def close_prompt(self) -> None:
        self._pending = None
        self._pending_kind = ""
        self._pending_json = "{}"
        self.stateChanged.emit()

    @Slot(bool)
    def decide(self, approved: bool) -> None:
        self._resolve("confirm", approved)

    @Slot(str)
    def answer(self, text: str) -> None:
        self._resolve("ask", text)

    def _resolve(self, kind: str, value: Any) -> None:
        """GUI 线程兑现界面上的授权/澄清决策。"""
        pending = self._pending
        if pending is None or pending[0] != kind:
            return
        future = pending[1]

        def _set() -> None:
            if not future.done():
                future.set_result(value)

        self._loop.call_soon_threadsafe(_set)

    # ---------- Core 开关 / 权限挡位 ----------

    @Slot(bool)
    def setCoreEnabled(self, on: bool) -> None:
        """停用专员时终止它在跑的任务，避免"无员工"形态下后台改写数据。"""
        self._enabled = bool(on)
        if not self._enabled:
            self.cancel()
        self.stateChanged.emit()

    @Slot(str)
    def setPermissionMode(self, mode: str) -> None:
        if mode not in PERMISSION_MODES:
            self.noticeRaised.emit(f"未知权限挡位 {mode}，可选 {PERMISSION_MODES}")
            return
        self._permission_mode = mode
        self.stateChanged.emit()

    def mark_unread_if_idle(self, event_type: str) -> None:
        """非当前专员的事件不打扰画面，只在列表上记一笔。"""
        if event_type in ("done", "error", "permission_asked", "clarify_requested"):
            self._unread += 1
            self.unreadChanged.emit(self._unread)
            self.stateChanged.emit()
