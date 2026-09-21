"""host_e 唯一的 Core 嵌入点（Single Integration Point）。

本文件是 host_e 里**唯一直接 import yai_core 的模块**，用来证明"应用 AI 化只需一个嵌入点"：

- ``crm_app.py`` 是纯业务系统（销售 CRM），**零 yai_core 依赖**，没有 Core 也能独立运行；
- ``web_app.py`` 是纯 HTTP 壳，只通过 :class:`AgentBridge` 的高层方法间接使用 Core，
  自身不出现任何 yai_core 符号；
- 想把数字员工从这个 CRM 里拿掉，只需停用本桥接（前端一键开关），业务系统毫发无损。

权限挡位（``permission_mode``）：

- ``manual``  全部审批：任何工具（含读工具、新发现的工具）调用前都要用户确认；
- ``partial`` 部分审批：白名单（读工具）自动放行，写工具 / 新发现工具询问（默认）；
- ``auto``    无需审批：全部自动放行，仅建议在本地演示时使用。

Core 开关（``enabled``）：关闭时桥接不启动任何 Agent 任务，前端回到"纯 CRM"形态，
这是"同一软件、有无 Core"对比的后端支撑。
"""

from __future__ import annotations

import asyncio
import queue
import threading
import uuid

from host_e_sales_crm.catalog_capabilities import build_catalog, catalog_summary
from yai_core import AgentCore, EventType, discover
from yai_core.policy import AllowlistPolicy

# 交互式授权/澄清的等待上限：超时按"拒绝/空回答"处理，避免任务永久挂起。
PROMPT_TIMEOUT_SECONDS = 600
# 内存中最多保留的历史会话（SSE 重连/取结果用），单用户本地演示足够。
MAX_RUNS = 10

# 读工具自动放行（partial 挡位白名单）；写工具（add_*/update_*/create_*/complete_*）一律先问人。
READ_TOOLS = [
    "list_customers", "search_customers", "get_customer",
    "list_orders", "sum_amount", "list_opportunities",
    "list_followups", "customers_due_followup", "list_todos", "daily_brief",
]

# 权限挡位：值 -> 前端展示名。
PERMISSION_MODES = ("manual", "partial", "auto")
PERMISSION_LABELS = {
    "manual": "全部审批",
    "partial": "部分审批（读放行 · 写/新能力询问）",
    "auto": "无需审批（仅本地演示）",
}
DEFAULT_PERMISSION_MODE = "partial"


class WebChannel:
    """Channel SPI 的网页实现：emit 不做事（事件统一由 astream 推送），
    confirm/ask 挂起等待浏览器经 /api/decision、/api/answer 回传。"""

    def __init__(self, run: dict, loop: asyncio.AbstractEventLoop) -> None:
        self._run = run
        self._loop = loop

    async def emit(self, event) -> None:  # noqa: ANN001 - 事件统一走 astream
        return None

    async def ask(self, question: str) -> str:
        return await self._prompt("ask", {"question": question}, "")

    async def confirm(self, tool_name: str, arguments: dict) -> bool:
        payload = {"tool": tool_name, "arguments": arguments}
        return bool(await self._prompt("confirm", payload, False))

    async def _prompt(self, kind: str, data: dict, default):
        future = self._loop.create_future()
        self._run["pending"] = (kind, future)
        event_type = "clarify_request" if kind == "ask" else "permission_request"
        self._run["queue"].put({"type": event_type, "data": data})
        try:
            return await asyncio.wait_for(future, timeout=PROMPT_TIMEOUT_SECONDS)
        except TimeoutError:
            return default
        finally:
            self._run["pending"] = None


def resolve_pending(run: dict, kind: str, value) -> bool:
    """从 HTTP 线程安全地兑现浏览器的授权/回答决策。"""
    pending = run.get("pending")
    if not pending or pending[0] != kind or pending[1].done():
        return False
    pending[1].get_loop().call_soon_threadsafe(pending[1].set_result, value)
    return True


def event_type_name(event) -> str:
    event_type = event.type
    return event_type.value if hasattr(event_type, "value") else str(event_type)


class AgentBridge:
    """把 YAI Agent Core 装配到 SalesCrm 上的唯一适配层。

    上层（web_app / CLI）只调用这里的高层方法，不直接接触 AgentCore。
    """

    def __init__(
        self,
        crm,
        model_factory,
        model_label: str = "",
        *,
        catalog=None,
        enabled: bool = True,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
    ) -> None:
        self.crm = crm
        self._model_factory = model_factory
        self.model_label = model_label
        # 按需能力目录：默认不注册，命中能力缺口才被发现并在本轮启用。
        self.catalog = catalog or build_catalog()
        # Core 开关：默认启用（开箱即用）；关闭后 start_run 拒绝新任务。
        self.enabled = enabled
        # 权限挡位，可在运行时切换，对下一次任务生效。
        self.set_permission_mode(permission_mode)
        self._loop = asyncio.new_event_loop()
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._run_loop, name="yai-web-loop", daemon=True).start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ---------- Core 开关 ----------

    def set_enabled(self, on: bool) -> bool:
        """启用/停用 Core。停用时若有任务在跑，先终止，避免在"纯 CRM"形态下后台改写数据。"""
        with self._lock:
            self.enabled = bool(on)
        if not on:
            active_id = self.active_id()
            if active_id:
                self.cancel(active_id)
        return self.enabled

    # ---------- 权限挡位 ----------

    def set_permission_mode(self, mode: str) -> str:
        if mode not in PERMISSION_MODES:
            raise ValueError(f"未知权限挡位 {mode!r}，可选：{PERMISSION_MODES}")
        self.permission_mode = mode
        return mode

    def _make_policy(self) -> AllowlistPolicy:
        if self.permission_mode == "manual":
            # 空白名单 + auto：白名单外全部 ASK，即任何工具都要审批。
            return AllowlistPolicy((), mode="auto")
        if self.permission_mode == "auto":
            # 全部自动放行（仅本地演示）。
            return AllowlistPolicy(READ_TOOLS, mode="allow_all")
        # partial（默认）：读工具放行，其余询问。
        return AllowlistPolicy(READ_TOOLS, mode="auto")

    # ---------- 只读状态 ----------

    def tools(self) -> list[dict]:
        result = []
        for spec in discover(self.crm):
            source = getattr(spec, "source", "native")
            source = source.value if hasattr(source, "value") else str(source)
            result.append({
                "name": spec.name,
                "description": spec.description,
                "source": source,
                "access": "read" if spec.name in READ_TOOLS else "write",
            })
        return result

    def ai_state(self) -> dict:
        with self._lock:
            active = self.active_id_locked()
        return {
            "core_enabled": self.enabled,
            "permission_mode": self.permission_mode,
            "permission_label": PERMISSION_LABELS[self.permission_mode],
            "model_label": self.model_label,
            "tools": self.tools(),
            "catalog": catalog_summary(),
            "active": active,
        }

    def active_id(self) -> str | None:
        with self._lock:
            return self.active_id_locked()

    def active_id_locked(self) -> str | None:
        for run_id, run in self._runs.items():
            if run["active"]:
                return run_id
        return None

    # ---------- 会话生命周期 ----------

    def start_run(self, task: str) -> str | None:
        """启动一次 Agent 任务。Core 关闭时返回 None（上层应回 503）。"""
        with self._lock:
            if not self.enabled:
                return None
            if self.active_id_locked():
                return None
            run_id = uuid.uuid4().hex[:12]
            run: dict = {
                "queue": queue.Queue(), "pending": None, "active": True,
                "task_text": task, "task": None,
            }
            self._runs[run_id] = run
            self._prune_locked()
        channel = WebChannel(run, self._loop)
        core = AgentCore.auto(
            self.crm,
            self._model_factory(),
            channel=channel,
            policy=self._make_policy(),
            llm_router="auto",
            discovery=self.catalog,
            composition=True,  # 允许模型把现有工具编排成组合工具（创建仍需授权）
        )
        asyncio.run_coroutine_threadsafe(self._run_guarded(core, task, run), self._loop)
        return run_id

    def get_run(self, run_id: str) -> dict | None:
        return self._runs.get(run_id)

    def resolve(self, run_id: str, kind: str, value) -> bool:
        run = self._runs.get(run_id)
        if not run:
            return False
        return resolve_pending(run, kind, value)

    def cancel(self, run_id: str) -> bool:
        """终止指定会话：兑现挂起的授权/澄清，并取消后台 Agent 任务。"""
        run = self._runs.get(run_id)
        if not run or not run["active"]:
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
            self._runs = {k: v for k, v in self._runs.items() if v is not run}

    async def _run_guarded(self, core: AgentCore, task: str, run: dict) -> None:
        run["task"] = asyncio.current_task()
        cancelled = False
        final_text = ""
        strategy = None
        try:
            async for event in core.astream(task):
                run["queue"].put({"type": event_type_name(event), "data": event.data})
                if event.type == EventType.DONE:
                    final_text = event.data.get("final_text", "")
                    strategy = event.data.get("strategy")
        except asyncio.CancelledError:
            cancelled = True
            run["queue"].put({
                "type": "cancelled",
                "data": {"reason": "任务已被用户终止"},
            })
            raise
        except Exception as exc:  # noqa: BLE001 - 网页端必须看到错误而不是白屏
            run["queue"].put({"type": "error", "data": {"error": f"{type(exc).__name__}: {exc}"}})
        finally:
            run["active"] = False
            run["task"] = None
            if not cancelled:
                run["queue"].put({
                    "type": "done",
                    "data": {"final_text": final_text, "strategy": strategy},
                })
