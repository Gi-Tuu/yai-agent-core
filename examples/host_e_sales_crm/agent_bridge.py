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
import json
import queue
import threading
import uuid
from pathlib import Path

from host_e_sales_crm.catalog_capabilities import build_catalog, catalog_summary
from yai_core import AgentCore, ContextualBanditSelector, EventType, discover
from yai_core.policy import AllowlistPolicy

# 交互式授权/澄清的等待上限：超时按"拒绝/空回答"处理，避免任务永久挂起。
PROMPT_TIMEOUT_SECONDS = 600
# 内存中最多保留的历史会话（SSE 重连/取结果用），单用户本地演示足够。
MAX_RUNS = 10
# 路由自学习状态文件（跨任务累积、重启不丢）；运行时生成，不入库。
_LEARNING_FILE = Path(__file__).parent / "route_learning.json"
# 学习器固定随机种子：演示可复现（探索仍受 epsilon 控制）。
_LEARNING_SEED = 42

# 上下文桶 key（见 learning/features.py 的 TaskFeatures.key）各维度中文名，
# 供学习面板把 9 维 tuple 渲染成人类可读标签。
_LENGTH_LABELS = {"short": "短任务", "medium": "中等任务", "long": "长任务", "empty": "空任务"}
_TOOLS_LABELS = {"none": "无工具", "few": "工具少", "many": "工具多"}
# 策略名中文展示。
_STRATEGY_LABELS = {
    "direct": "直接回答", "react": "工具推理", "plan": "先规划", "clarify": "先澄清",
}

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


def load_learning(
    path: str | Path | None = None, *, seed: int = _LEARNING_SEED
) -> ContextualBanditSelector:
    """加载共享路由学习器（网页 Workbench 与终端 run_agent 共用同一状态文件）。

    文件不存在或损坏时冷启动一个新选择器（纯规则先验），不影响宿主启动。
    """
    p = Path(path) if path is not None else _LEARNING_FILE
    try:
        if p.exists():
            return ContextualBanditSelector.load(p, seed=seed)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass  # 状态不可读就从规则先验重新开始
    return ContextualBanditSelector(seed=seed)


def save_learning(selector: ContextualBanditSelector, path: str | Path | None = None) -> bool:
    """把学习状态持久化到共享文件；失败不致命（演示可继续）。"""
    p = Path(path) if path is not None else _LEARNING_FILE
    try:
        selector.save(p)
        return True
    except OSError:
        return False


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
        learning_path: str | Path | None = None,
        enable_learning: bool = True,
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
        # 路由自学习：跨任务共享的同一个选择器，每个任务的 Core 都挂它，
        # 任务结束后落盘，重启不丢——这是"路由真的在学习"的接线点。
        self._learning_path = Path(learning_path) if learning_path else _LEARNING_FILE
        self._learning = self._build_learning() if enable_learning else None
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

    # ---------- 路由自学习 ----------

    def _build_learning(self) -> ContextualBanditSelector:
        """启动时加载历史学习状态；文件缺失或损坏则冷启动（不影响启动）。"""
        return load_learning(self._learning_path, seed=_LEARNING_SEED)

    def _save_learning(self) -> None:
        """任务结束后在后台事件循环线程落盘（状态很小，一次 JSON 写）。"""
        if self._learning is not None:
            save_learning(self._learning, self._learning_path)

    def reset_learning(self) -> bool:
        """清空学习状态（回到规则先验），供前端"重置学习"按钮调用。"""
        if self._learning is None:
            return False
        self._learning = ContextualBanditSelector(seed=_LEARNING_SEED)
        self._save_learning()
        return True

    async def _learning_snapshot(self) -> dict:
        """在后台事件循环线程内取快照，避免与 record 并发遍历状态表。"""
        if self._learning is None:
            return {"enabled": False}
        return self._summarize_learning(self._learning.to_dict())

    def learning_status(self) -> dict:
        """线程安全地读取学习状态（HTTP 线程调用，转发到后台 loop）。"""
        if self._learning is None:
            return {"enabled": False}
        future = asyncio.run_coroutine_threadsafe(self._learning_snapshot(), self._loop)
        try:
            return future.result(timeout=5)
        except Exception:  # noqa: BLE001 - 面板读取失败不应让前端报错
            return {"enabled": True, "available": False}

    @staticmethod
    def _describe_context(parts: list) -> str:
        """把 9 维上下文桶 key 渲染成简短中文标签。"""
        length, action, multistep, vague, question, has_latin, data_obj, unspec, tools = parts
        if multistep:
            kind = "多步任务"
        elif vague or unspec:
            kind = "需澄清"
        elif question:
            kind = "疑问查询"
        elif action:
            kind = "单步行动"
        else:
            kind = "一般对话"
        extras = []
        if data_obj:
            extras.append("含数据对象")
        if has_latin:
            extras.append("含英文")
        tail = "·".join(extras)
        return "·".join(
            x for x in (_LENGTH_LABELS.get(length, str(length)), kind,
                        _TOOLS_LABELS.get(tools, str(tools)), tail) if x
        )

    def _summarize_learning(self, data: dict) -> dict:
        """把学习器原始状态转成面板友好的结构：总观测数 + 各桶偏好。"""
        state = data.get("state", {})
        obs = data.get("obs", {})
        buckets = []
        total_obs = 0
        for key, table in state.items():
            observed = int(obs.get(key, 0))
            total_obs += observed
            means = {
                arm: (a / (a + b) if (a + b) > 0 else 0.0)
                for arm, (a, b) in table.items()
            }
            preferred = max(means, key=lambda arm: means[arm])
            buckets.append({
                "context": self._describe_context(json.loads(key)),
                "observed": observed,
                "preferred": preferred,
                "preferred_label": _STRATEGY_LABELS.get(preferred, preferred),
                "confidence": round(means[preferred], 3),
                "means": {
                    arm: {"label": _STRATEGY_LABELS.get(arm, arm), "mean": round(m, 3)}
                    for arm, m in means.items()
                },
            })
        buckets.sort(key=lambda b: (-b["observed"], b["context"]))
        return {
            "enabled": True,
            "selector": "ContextualBanditSelector",
            "learned_tasks": total_obs,
            "context_buckets": len(state),
            "buckets": buckets,
        }

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
            learning=self._learning,  # 跨任务共享的自学习器；None 时内核行为不变
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
            # 正常结束（非取消）时内核已回灌本次反馈，这里落盘让学习跨重启保留。
            if not cancelled:
                self._save_learning()
                run["queue"].put({
                    "type": "done",
                    "data": {"final_text": final_text, "strategy": strategy},
                })
