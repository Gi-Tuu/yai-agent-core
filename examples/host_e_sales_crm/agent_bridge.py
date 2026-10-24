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
import hashlib
import json
import queue
import threading
import uuid
from dataclasses import replace
from pathlib import Path

from host_e_sales_crm.catalog_capabilities import build_catalog, catalog_summary
from yai_core import (
    AgentCore,
    ChangePreview,
    ContextualBanditSelector,
    EventType,
    discover,
)
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
WRITE_TOOL_NAMES = {
    "add_customer", "update_customer", "add_followup",
    "create_order", "create_opportunity", "update_opportunity_stage",
    "create_todo", "complete_todo",
    "delete_todo", "delete_order", "delete_opportunity", "delete_customer",
}


def _require_customer(crm, ref: object, *, company: bool = False) -> dict:
    text = str(ref or "").strip()
    if not text:
        raise ValueError("客户不能为空")
    found = crm._resolve_customer(text) if company else crm._find_customer(text)
    if found is None:
        raise ValueError(f"客户 {text!r} 不存在")
    return found


def _tool_state(crm, tool_name: str, arguments: dict):
    """审批依据：只取这次写操作真正会读到的状态。

    全量快照会让"另一张表改了一行"也把审批打成失效——审批卡开着时去补个待办，
    回来就被告知依据已变。按工具圈定后，无关变更不影响 token，同一目标的变化仍会。
    """
    data = crm._data
    if tool_name == "add_followup":
        customer = _require_customer(crm, arguments.get("customer"))
        return {
            "customer": customer,
            "followups": [f for f in data["followups"] if f["customer"] == customer["name"]],
        }
    if tool_name == "create_todo":
        # 新增待办不读已有待办（也不做重名检查），参数本身就是全部依据。
        return {}
    if tool_name == "add_customer":
        # 只关心"会不会撞名"：重名检查做精确 + 双向包含匹配。
        name = str(arguments.get("name", "")).strip()
        return {
            "conflicts": [
                c["name"]
                for c in data["customers"]
                if name and (name in c["name"] or c["name"] in name)
            ]
        }
    if tool_name == "update_customer":
        return {"customer": _require_customer(crm, arguments.get("name"))}
    if tool_name == "create_order":
        # 客户决定落到哪家公司，现有订单决定新订单号。
        return {
            "customer": _require_customer(crm, arguments.get("customer"), company=True),
            "orders": data["orders"],
        }
    if tool_name == "create_opportunity":
        # 只有同名的已存在商机会改变结果；客户决定落到哪家公司。
        title = str(arguments.get("title", "")).strip()
        return {
            "customer": _require_customer(crm, arguments.get("customer"), company=True),
            "title_taken": any(o["title"] == title for o in data["opportunities"]),
        }
    if tool_name == "update_opportunity_stage":
        title = str(arguments.get("title", "")).strip()
        return {"matches": [o for o in data["opportunities"] if title in o["title"]]}
    if tool_name == "complete_todo":
        title = str(arguments.get("title", "")).strip()
        return {
            "open_matches": [
                t for t in data["todos"] if not t["done"] and title in t["title"]
            ]
        }
    if tool_name == "delete_todo":
        title = str(arguments.get("title", "")).strip()
        return {"matches": [t for t in data["todos"] if title in t["title"]]}
    if tool_name == "delete_order":
        wanted = str(arguments.get("order", "")).strip()
        return {"matches": [o for o in data["orders"] if wanted in str(o.get("order", ""))]}
    # 其余写工具没有专属预览器：保守起见仍按全量数据判定。
    return {"data": data}


def _revision(crm, tool_name: str, arguments: dict) -> str:
    """预览 token：工具 + 本次会读到的状态 + 参数；无关变更不换 token。"""
    # 这是示例宿主，直接读取业务对象；真实系统应调用自己的 revision/事务快照。
    raw = json.dumps(
        {
            "tool": tool_name,
            "state": _tool_state(crm, tool_name, arguments),
            "arguments": arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _render_write_preview(crm, tool_name: str, arguments: dict) -> ChangePreview:
    """宿主侧真实预览：读当前业务对象，渲染这次写操作会造成什么变化。"""
    args = dict(arguments or {})
    token = _revision(crm, tool_name, args)
    target = ""
    if tool_name == "add_followup":
        customer = _require_customer(crm, args.get("customer"))
        content = str(args.get("content", "")).strip()
        if not content:
            raise ValueError("跟进内容不能为空")
        summary = f"为 {customer['name']} 新增一条跟进"
        diff = (
            f"+ 跟进内容：{content}\n"
            f"最近跟进：{customer['last_followup']} -> {crm._clock().isoformat()}"
        )
        target = f"customer/{customer['name']}"
    elif tool_name == "create_todo":
        title = str(args.get("title", "")).strip()
        if not title:
            raise ValueError("待办标题不能为空")
        due = str(args.get("due", "")).strip()
        summary = f"新增待办：{title}"
        diff = f"+ 待办：{title}\n  截止：{due or '无'}"
        target = f"todo/{title}"
    elif tool_name == "add_customer":
        name = str(args.get("name", "")).strip()
        if not name:
            raise ValueError("客户姓名不能为空")
        if crm._find_customer(name) is not None:
            raise ValueError(f"客户 {name!r} 已存在")
        summary = f"新增客户：{name}"
        diff = (
            f"+ 姓名：{name}\n+ 公司：{args.get('company', '')}\n"
            f"+ 等级：{args.get('level', '普通')}\n+ 状态：潜在"
        )
        target = f"customer/{name}"
    elif tool_name == "update_customer":
        customer = _require_customer(crm, args.get("name"))
        changes = []
        for key in ("company", "level", "phone", "status"):
            value = str(args.get(key, "") or "").strip()
            if value:
                changes.append(f"  {key}: {customer.get(key, '')} -> {value}")
        if not changes:
            raise ValueError("没有需要更新的字段")
        summary = f"编辑客户：{customer['name']}"
        diff = "\n".join(changes)
        target = f"customer/{customer['name']}"
    elif tool_name == "create_order":
        customer = _require_customer(crm, args.get("customer"), company=True)
        summary = f"为 {customer['company']} 录入订单"
        diff = (
            f"+ 客户：{customer['company']}\n"
            f"+ 品类：{args.get('category', '')}\n"
            f"+ 金额：¥{args.get('amount', '')}\n"
            f"+ 区域：{args.get('region', '') or '未填'}"
        )
        target = f"order/new/{customer['company']}"
    elif tool_name == "create_opportunity":
        customer = _require_customer(crm, args.get("customer"), company=True)
        summary = f"新建商机：{args.get('title', '')}"
        diff = (
            f"+ 商机：{args.get('title', '')}\n+ 客户：{customer['company']}\n"
            f"+ 金额：¥{args.get('amount', '')}\n+ 阶段：{args.get('stage', '初步接触')}"
        )
        target = f"opportunity/{args.get('title', '')}"
    elif tool_name == "update_opportunity_stage":
        title = str(args.get("title", "")).strip()
        old = next((o for o in crm._data["opportunities"] if title in o["title"]), None)
        if old is None:
            raise ValueError(f"没有找到标题包含 {title!r} 的商机")
        summary = f"推进商机：{old['title']}"
        diff = f"  阶段：{old['stage']} -> {args.get('stage', '')}"
        target = f"opportunity/{old['title']}"
    elif tool_name == "complete_todo":
        title = str(args.get("title", "")).strip()
        todo = next(
            (t for t in crm._data["todos"] if not t["done"] and title in t["title"]),
            None,
        )
        if todo is None:
            raise ValueError(f"没有找到标题包含 {title!r} 的未完成待办")
        summary = f"完成待办：{todo['title']}"
        diff = "  完成状态：未完成 -> 已完成"
        target = f"todo/{todo['title']}"
    elif tool_name in ("delete_todo", "delete_order"):
        key = "todos" if tool_name == "delete_todo" else "orders"
        field = "title" if tool_name == "delete_todo" else "order"
        wanted = str(args.get(field, "")).strip()
        row = next((x for x in crm._data[key] if wanted in str(x.get(field, ""))), None)
        if row is None:
            raise ValueError(f"没有找到匹配的{ '待办' if key == 'todos' else '订单' }")
        summary = f"删除{ '待办' if key == 'todos' else '订单' }：{row.get(field)}"
        diff = f"- {json.dumps(row, ensure_ascii=False, sort_keys=True)}"
        target = f"{key}/{row.get(field)}"
    else:
        summary = f"执行写操作：{tool_name}"
        diff = json.dumps(args, ensure_ascii=False, sort_keys=True)
    return ChangePreview(summary=summary, token=token, diff=diff, target=target)


def render_write_preview(crm, tool_name: str, arguments: dict) -> ChangePreview:
    """公开的预览渲染入口：网页/桌面跨进程调用与本地闭包共用同一份逻辑。"""
    return _render_write_preview(crm, tool_name, arguments)


def build_write_preview(crm, tool_name: str):
    """构造某写工具的 preview 闭包（可单测，不需要启动 AgentBridge）。"""

    def preview(arguments: dict, subject) -> ChangePreview:
        return render_write_preview(crm, tool_name, arguments)

    return preview


def attach_write_previews(core: AgentCore, crm) -> None:
    """给已注册写工具挂上宿主预览器；只改治理对象，不改业务方法。"""
    for spec in core.registry.all():
        if spec.name in WRITE_TOOL_NAMES:
            spec.governance = replace(
                spec.governance, preview=build_write_preview(crm, spec.name)
            )

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

    #: 接收宿主预览与审批描述，让网页确认卡显示真实变更而不是只有工具名。
    yai_approval_context_aware = True

    def __init__(self, run: dict, loop: asyncio.AbstractEventLoop) -> None:
        self._run = run
        self._loop = loop

    async def emit(self, event) -> None:  # noqa: ANN001 - 事件统一走 astream
        return None

    async def ask(self, question: str) -> str:
        return await self._prompt("ask", {"question": question}, "")

    async def confirm(
        self, tool_name: str, arguments: dict, context: dict | None = None
    ) -> bool:
        payload = {"tool": tool_name, "arguments": arguments}
        for key in ("approval_required", "reason", "prompt", "risk", "scope", "target", "preview"):
            if context and key in context:
                payload[key] = context[key]
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
        state_provider=None,
    ) -> None:
        self.crm = crm
        self._model_factory = model_factory
        self.model_label = model_label
        # 宿主现状锚（如网页当前选中的客户）；None = 不注入。
        self._state_provider = state_provider
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
        attach_write_previews(core, self.crm)
        if self._state_provider is not None:
            core.register_state_provider("sales_selection", self._state_provider)
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
