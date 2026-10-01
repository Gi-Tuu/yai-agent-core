"""工作台运行时：一个灵动岛管理多个彼此隔离的专员。

定位纪律（务必守住）：灵动岛**不是**聚合所有工具的大 Core，它只做四件事——
连接各应用内置的 Core、切换当前专员、显示该专员的事件、转发用户的授权/回答。

- **唯一一条后台 asyncio 线程**由工作台持有，全部专员共用（不为专员膨胀线程）；
- QML 只连工作台：读聚合 Property、发统一 ``specialistEvent(sid, type, payload)``；
- 非当前专员的事件不改画面，只在切换条上记未读角标。
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    Signal,
    Slot,
)

from shell.desktop.specialist import (
    PERMISSION_MODES,
    SpecialistRuntime,
    SpecialistSpec,
)

#: 专员列表的 QML 角色表。
_ROLES = {
    Qt.UserRole + 1: b"specialistId",
    Qt.UserRole + 2: b"name",
    Qt.UserRole + 3: b"glyph",
    Qt.UserRole + 4: b"busy",
    Qt.UserRole + 5: b"pendingKind",
    Qt.UserRole + 6: b"unread",
    Qt.UserRole + 7: b"enabled",
    Qt.UserRole + 8: b"active",
    Qt.UserRole + 9: b"runMode",
}


class SpecialistListModel(QAbstractListModel):
    """给 QML 专员切换条的列表模型（一行一个专员）。"""

    def __init__(self, workbench: WorkbenchRuntime, parent=None) -> None:
        super().__init__(parent)
        self._workbench = workbench
        self._by_role = {role: name.decode() for role, name in _ROLES.items()}

    def roleNames(self) -> dict:
        return _ROLES

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(self._workbench.specialists)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        specialists = self._workbench.specialists
        if not index.isValid() or not (0 <= index.row() < len(specialists)):
            return None
        item = specialists[index.row()]
        key = self._by_role.get(role)
        if key == "specialistId":
            return item.sid
        if key == "name":
            return item.spec.name
        if key == "glyph":
            return item.spec.glyph
        if key == "busy":
            return item.busy
        if key == "pendingKind":
            return item.pending_kind
        if key == "unread":
            return item.unread
        if key == "enabled":
            return item.enabled
        if key == "runMode":
            return item.run_mode
        if key == "active":
            return item.sid == self._workbench.active_id
        return None

    def refresh(self, sid: str) -> None:
        """专员状态变化时刷新对应行（在 GUI 线程调用）。"""
        for row, item in enumerate(self._workbench.specialists):
            if item.sid == sid:
                top = self.index(row, 0)
                self.dataChanged.emit(top, top, list(_ROLES))
                return

    def refresh_all(self) -> None:
        """切换当前专员时全部刷新（新旧两行的 active 都要跟着变）。"""
        if not self._workbench.specialists:
            return
        self.dataChanged.emit(self.index(0, 0),
                              self.index(len(self._workbench.specialists) - 1, 0),
                              list(_ROLES))


class WorkbenchRuntime(QObject):
    """多专员容器：共享 loop + 当前专员聚合 + 事件汇聚（带 sid）。"""

    stateChanged = Signal()
    #: (专员 id, 事件类型, JSON 负载)
    specialistEvent = Signal(str, str, str)
    noticeRaised = Signal(str)
    quitRequested = Signal()
    #: 当前专员的代码工具仓库刷新（payload = status JSON 字符串）
    codeVaultUpdated = Signal(str)
    #: 当前专员的路由学习状态刷新（payload = summary JSON 字符串）
    learningUpdated = Signal(str)

    def __init__(
        self,
        specs: list[SpecialistSpec],
        *,
        mode: str = "live",
        active_id: str | None = None,
        permission_mode: str = "partial",
    ) -> None:
        super().__init__()
        if not specs:
            raise ValueError("工作台至少需要一个专员")
        self._specialists: dict[str, SpecialistRuntime] = {}
        self._order: list[str] = []
        self._active_id = active_id or specs[0].id
        self._lock = threading.Lock()
        self._vault_json = "{}"
        self._learning_json = "{}"
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="yai-workbench-loop", daemon=True
        )
        self._thread.start()
        self.model = SpecialistListModel(self, parent=self)
        for spec in specs:
            self.add_specialist(spec, permission_mode=permission_mode, mode=mode)

    # ---------- 装配 ----------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def add_specialist(
        self,
        spec: SpecialistSpec,
        *,
        permission_mode: str = "partial",
        mode: str = "live",
    ) -> SpecialistRuntime:
        """注册一个专员（live 形态缺真 Core 时回退演示脚本并标注）。"""
        live = mode == "live" and spec.live_factory is not None
        item = SpecialistRuntime(
            spec,
            self._loop,
            factory=spec.live_factory if live else spec.demo_factory,
            permission_mode=permission_mode,
            run_mode="live" if live else "demo",
        )
        item.eventReceived.connect(lambda name, payload, sid=spec.id:
                                   self._relay(sid, name, payload))
        item.stateChanged.connect(lambda sid=spec.id: self._on_state(sid))
        item.noticeRaised.connect(self.noticeRaised.emit)
        self._specialists[spec.id] = item
        self._order.append(spec.id)
        self.model.beginInsertRows(QModelIndex(), len(self._order) - 1,
                                   len(self._order) - 1)
        self.model.endInsertRows()
        self.stateChanged.emit()
        return item

    @property
    def specialists(self) -> list[SpecialistRuntime]:
        return [self._specialists[sid] for sid in self._order]

    @property
    def active_id(self) -> str:
        return self._active_id

    def get(self, sid: str) -> SpecialistRuntime:
        """按 id 取专员（宿主/测试用；QML 只走聚合 Property）。"""
        return self._specialists[sid]

    def _relay(self, sid: str, name: str, payload: str) -> None:
        """专员事件统一出口：当前专员上屏，其余只记未读。"""
        if sid != self._active_id:
            self._specialists[sid].mark_unread_if_idle(name)
        self.specialistEvent.emit(sid, name, payload)

    def _on_state(self, sid: str) -> None:
        self.model.refresh(sid)
        if sid == self._active_id:
            self.stateChanged.emit()

    def _active(self) -> SpecialistRuntime:
        return self._specialists[self._active_id]

    # ---------- 聚合 Property（QML 只读这些） ----------

    def _get_active_id(self) -> str:
        return self._active_id

    def _get_busy(self) -> bool:
        return self._active().busy

    def _get_pending_kind(self) -> str:
        return self._active().pending_kind

    def _get_pending_payload(self) -> str:
        return self._active().pending_payload

    def _get_core_enabled(self) -> bool:
        return self._active().enabled

    def _get_permission_mode(self) -> str:
        return self._active().permission_mode

    def _get_run_mode(self) -> str:
        return self._active().run_mode

    def _get_name(self) -> str:
        return self._active().spec.name

    def _get_glyph(self) -> str:
        return self._active().spec.glyph

    def _get_unread_total(self) -> int:
        return sum(item.unread for item in self.specialists)

    def _get_ids(self) -> list[str]:
        return list(self._order)

    def _get_model(self) -> QObject:
        return self.model

    @Slot(str, result=str)
    def displayName(self, sid: str) -> str:
        """按 id 取显示名（胶囊上提示"哪个专员有事找你"）。"""
        item = self._specialists.get(sid)
        return item.spec.name if item else sid

    activeSpecialistId = Property(str, _get_active_id, notify=stateChanged)
    activeBusy = Property(bool, _get_busy, notify=stateChanged)
    activePendingKind = Property(str, _get_pending_kind, notify=stateChanged)
    activePendingPayload = Property(str, _get_pending_payload, notify=stateChanged)
    activeCoreEnabled = Property(bool, _get_core_enabled, notify=stateChanged)
    activePermissionMode = Property(str, _get_permission_mode, notify=stateChanged)
    activeRunMode = Property(str, _get_run_mode, notify=stateChanged)
    activeName = Property(str, _get_name, notify=stateChanged)
    activeGlyph = Property(str, _get_glyph, notify=stateChanged)
    unreadTotal = Property(int, _get_unread_total, notify=stateChanged)
    specialistIds = Property(list, _get_ids, notify=stateChanged)

    def _get_code_vault(self) -> str:
        return self._vault_json

    def _get_code_vault_available(self) -> bool:
        return self._active_storage() is not None

    def _get_learning(self) -> str:
        return self._learning_json

    def _get_learning_available(self) -> bool:
        return self._active_learning_path() is not None

    codeVault = Property(str, _get_code_vault, notify=codeVaultUpdated)
    codeVaultAvailable = Property(bool, _get_code_vault_available, notify=stateChanged)
    learningVault = Property(str, _get_learning, notify=learningUpdated)
    learningAvailable = Property(bool, _get_learning_available, notify=stateChanged)
    specialistModel = Property(QObject, _get_model, notify=stateChanged)

    # ---------- 转发到当前专员 ----------

    @Slot(str)
    def setActiveSpecialist(self, sid: str) -> None:
        if sid not in self._specialists or sid == self._active_id:
            return
        self._active_id = sid
        self._specialists[sid].clear_unread()
        self.model.refresh_all()
        self.stateChanged.emit()

    @Slot(str, result=bool)
    def startTask(self, text: str) -> bool:
        return self._active().startTask(text)

    @Slot()
    def cancel(self) -> None:
        self._active().cancel()

    @Slot(bool)
    def decide(self, approved: bool) -> None:
        self._active().decide(approved)

    @Slot(str)
    def answer(self, text: str) -> None:
        self._active().answer(text)

    @Slot(bool)
    def setCoreEnabled(self, on: bool) -> None:
        self._active().setCoreEnabled(on)

    @Slot(str)
    def setPermissionMode(self, mode: str) -> None:
        self._active().setPermissionMode(mode)

    @Slot()
    def requestQuit(self) -> None:
        self.quitRequested.emit()

    # ---------- 代码工具仓库（读写派发到常驻 loop，与 Core 串行） ----------

    def _active_storage(self) -> Path | None:
        item = self._specialists.get(self._active_id)
        return None if item is None else item.spec.code_storage

    def _submit_vault(self, work, *args) -> None:
        """空闲时也能开面板：读写全部排到 worker loop，与运行中的 Core 同线程串行。"""
        path = self._active_storage()
        if path is None:
            return

        async def _coro() -> None:
            work(path, *args)

        asyncio.run_coroutine_threadsafe(_coro(), self._loop)

    def _emit_vault(self, data: dict) -> None:
        # 在 worker 线程写好再 emit；Qt 以 QueuedConnection 投递到 GUI 线程。
        self._vault_json = json.dumps(data, ensure_ascii=False, default=str)
        self.codeVaultUpdated.emit(self._vault_json)

    def _vault_refresh(self, path: Path) -> None:
        from shell.desktop.tool_vault import vault_status

        self._emit_vault(vault_status(path))

    def _vault_set_perm(self, path: Path, name: str, on: bool) -> None:
        from shell.desktop.tool_vault import vault_set_permanent, vault_status

        vault_set_permanent(path, name, on)
        self._emit_vault(vault_status(path))

    def _vault_remove(self, path: Path, name: str) -> None:
        from shell.desktop.tool_vault import vault_remove, vault_status

        vault_remove(path, name)
        self._emit_vault(vault_status(path))

    @Slot()
    def openCodeVault(self) -> None:
        self._submit_vault(self._vault_refresh)

    @Slot(str, bool)
    def setCodeToolPermanent(self, name: str, on: bool) -> None:
        self._submit_vault(self._vault_set_perm, name, on)

    @Slot(str)
    def removeCodeTool(self, name: str) -> None:
        self._submit_vault(self._vault_remove, name)

    # ---------- 路由学习状态（只读，派发到常驻 loop） ----------

    def _active_learning_path(self) -> Path | None:
        item = self._specialists.get(self._active_id)
        return None if item is None else item.spec.learning_path

    def _submit_learning(self, work) -> None:
        """空闲也能看：读取排到 worker loop，与运行中的 Core 同线程串行。"""
        path = self._active_learning_path()
        if path is None:
            return

        async def _coro() -> None:
            work(path)

        asyncio.run_coroutine_threadsafe(_coro(), self._loop)

    def _emit_learning(self, data: dict) -> None:
        # 在 worker 线程读好再 emit；Qt 以 QueuedConnection 投递到 GUI 线程。
        self._learning_json = json.dumps(data, ensure_ascii=False, default=str)
        self.learningUpdated.emit(self._learning_json)

    def _learning_refresh(self, path: Path) -> None:
        from shell.desktop.learning_vault import summarize

        self._emit_learning(summarize(path))

    @Slot()
    def openLearning(self) -> None:
        self._submit_learning(self._learning_refresh)

    def shutdown(self) -> None:
        """退出前收尾：取消并**排空**全部任务，再关掉唯一那条后台循环。

        直接 ``loop.stop()`` 会让正在取消的协程在对象销毁后才跑完 ``finally``，
        届时 emit 会报 "Signal source has been deleted"。
        """
        for item in self.specialists:
            item.cancel()

        async def _drain() -> None:
            me = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not me]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_drain(), self._loop).result(timeout=3)
        except (TimeoutError, RuntimeError):
            pass  # 排空超时也要继续收尾，别把线程留活
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)
        self._loop.close()


__all__ = ["PERMISSION_MODES", "SpecialistListModel", "WorkbenchRuntime"]
