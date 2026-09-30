"""ShellSession：员工薄壳的会话与事件中枢（通用、不绑定具体宿主）。

- 内部持有一个后台 asyncio 事件循环线程，内核运行都在该循环里；
- 每次任务对应一个 run：事件队列（供 SSE 拉取）+ 挂起决策（授权/澄清）+ 活动标志；
- ``stream_factory(task, channel)`` 决定事件来源，便于同一套会话支持两种形态：
  * 真内核模式：构建 ``AgentCore(channel=WebChannel)`` 并返回 ``core.astream(task)``；
  * 演示模式：返回脚本化事件迭代器（离线、无 API Key，用于核对全部动效）。
- Core 开关 / 权限挡位在此管理；浏览器的授权与澄清回答经 :meth:`resolve` 兑现。
"""

from __future__ import annotations

import asyncio
import queue
import threading
import uuid
from collections.abc import AsyncIterator, Callable

from shell.webchannel import WebChannel

#: 内存中保留的历史任务数（SSE 重连/取结果用），单用户本地演示足够。
MAX_RUNS = 10

#: 权限挡位：全部审批 / 部分审批 / 无需审批。
PERMISSION_MODES = ("manual", "partial", "auto")

#: 事件流工厂：(原始任务, 网页通道) -> 事件异步迭代器。
StreamFactory = Callable[[str, object], AsyncIterator[object]]

#: 终结型事件：SSE 见到它们即关闭本次流。
_TERMINAL_EVENTS = ("done", "error", "cancelled")


def event_type_name(event: object) -> str:
    """取事件类型字符串（兼容 StrEnum 与普通字符串）。"""
    event_type = getattr(event, "type", None)
    return event_type.value if hasattr(event_type, "value") else str(event_type)


def resolve_pending(run: dict, kind: str, value: object) -> bool:
    """从 HTTP 线程安全地兑现浏览器的授权/澄清决策。"""
    pending = run.get("pending")
    if not pending or pending[0] != kind or pending[1].done():
        return False
    pending[1].get_loop().call_soon_threadsafe(pending[1].set_result, value)
    return True


class ShellSession:
    """管理后台事件循环、任务生命周期、Core 开关与权限挡位。"""

    def __init__(
        self,
        stream_factory: StreamFactory,
        *,
        enabled: bool = True,
        permission_mode: str = "partial",
        mode: str = "live",
    ) -> None:
        if permission_mode not in PERMISSION_MODES:
            raise ValueError(
                f"未知权限挡位 {permission_mode!r}，可选 {PERMISSION_MODES}"
            )
        self._stream_factory = stream_factory
        self.enabled = enabled
        self.permission_mode = permission_mode
        self.mode = mode
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        threading.Thread(
            target=self._run_loop, name="yai-shell-loop", daemon=True
        ).start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ---------- Core 开关 / 权限挡位 ----------

    def set_enabled(self, on: bool) -> bool:
        """启用/停用 Core；停用时终止在跑任务，避免"纯宿主"形态下后台改写数据。"""
        with self._lock:
            self.enabled = bool(on)
        if not on:
            active_id = self.active_id()
            if active_id:
                self.cancel(active_id)
        return self.enabled

    def set_permission_mode(self, mode: str) -> str:
        """切换权限挡位，对下一次任务生效。"""
        if mode not in PERMISSION_MODES:
            raise ValueError(f"未知权限挡位 {mode!r}，可选 {PERMISSION_MODES}")
        self.permission_mode = mode
        return mode

    def state(self) -> dict:
        """前端/探针读取的薄壳状态。"""
        return {
            "core_enabled": self.enabled,
            "permission_mode": self.permission_mode,
            "mode": self.mode,
            "active": self.active_id(),
        }

    # ---------- 任务查询 ----------

    def active_id(self) -> str | None:
        with self._lock:
            return self._active_id_locked()

    def _active_id_locked(self) -> str | None:
        for run_id, run in self._runs.items():
            if run["active"]:
                return run_id
        return None

    def get_run(self, run_id: str) -> dict | None:
        return self._runs.get(run_id)

    # ---------- 任务生命周期 ----------

    def start_run(self, task: str) -> tuple[str | None, str | None]:
        """启动一次任务，返回 ``(run_id, 错误码)``；错误码见返回处。"""
        text = task.strip()
        with self._lock:
            if not self.enabled:
                return None, "core_disabled"
            if self._active_id_locked():
                return None, "busy"
            if not text:
                return None, "empty"
            run_id = uuid.uuid4().hex[:12]
            run: dict = {
                "queue": queue.Queue(),
                "pending": None,
                "active": True,
                "ended": False,
                "task_text": text,
                "task": None,
            }
            self._runs[run_id] = run
            self._prune_locked()
        channel = WebChannel(run, self._loop)
        asyncio.run_coroutine_threadsafe(
            self._run_guarded(run, text, channel), self._loop
        )
        return run_id, None

    def resolve(self, run_id: str, kind: str, value: object) -> bool:
        """兑现浏览器对某次任务的决策（kind 为 'confirm' 或 'ask'）。"""
        run = self._runs.get(run_id)
        if run is None:
            return False
        return resolve_pending(run, kind, value)

    def cancel(self, run_id: str) -> bool:
        """终止某次任务：兑现挂起决策并取消后台协程。"""
        run = self._runs.get(run_id)
        if run is None or not run["active"]:
            return False
        task = run.get("task")
        pending = run.get("pending")

        def _do_cancel() -> None:
            if pending and not pending[1].done():
                kind, future = pending
                if not future.done():
                    future.set_result(False if kind == "confirm" else "")
            if task is not None:
                task.cancel()

        self._loop.call_soon_threadsafe(_do_cancel)
        return True

    def _prune_locked(self) -> None:
        finished = [r for r in self._runs.values() if not r["active"]]
        for run in finished[:-MAX_RUNS]:
            self._runs = {
                k: v for k, v in self._runs.items() if v is not run
            }

    async def _run_guarded(
        self, run: dict, task: str, channel: WebChannel
    ) -> None:
        run["task"] = asyncio.current_task()
        cancelled = False
        try:
            async for event in self._stream_factory(task, channel):
                name = event_type_name(event)
                run["queue"].put(
                    {"type": name, "data": getattr(event, "data", {})}
                )
                if name in _TERMINAL_EVENTS:
                    run["ended"] = True
                    if name == "done":
                        return
        except asyncio.CancelledError:
            cancelled = True
            run["ended"] = True
            run["queue"].put(
                {"type": "cancelled", "data": {"reason": "任务已被用户终止"}}
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 网页端需看到错误而非白屏
            run["ended"] = True
            run["queue"].put(
                {
                    "type": "error",
                    "data": {"error": f"{type(exc).__name__}: {exc}"},
                }
            )
        finally:
            run["active"] = False
            run["task"] = None
            #流自然结束但未显式给 done：补一个终结事件，避免 SSE 挂住。
            if not cancelled and not run["ended"]:
                run["queue"].put({"type": "done", "data": {}})
