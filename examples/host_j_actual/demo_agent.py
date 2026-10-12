"""host_j 的 Core 装配点（命令行与网页共用的唯一 Agent 接线处）。

宿主是一个极简记账应用（Actual Budget 形态）：默认只有查账/预算等只读工具，
**没有**"未分类交易自动归类"能力。这里把 YAI Agent Core 接进来，用两条离线
确定性链路完整演示第三方播种的"五拍"：

- :class:`ReconcileModel`（默认，发现+授权链路）：月底对账长尾任务 → 路由报告
  缺少"交易自动分类建议" → 内核从目录发现 ``suggest_category`` 并在部分审批档
  下经用户授权 → 拉交易、出建议、批量补分类（写操作再次授权）→ 核对预算 →
  给出月度小结。
- :class:`DelegatingReconcileModel`（``--delegate``，委派链路）：根识别到两个
  彼此独立的只读子目标，调 ``delegate`` 并行派两个**白名单收窄到单一工具**的
  短命子员工，结果只回流根，由根汇总。该模型无状态、只看消息，多个子员工在
  ``asyncio.gather`` 下交错调用也保持确定。

真实模型（DeepSeek / Agnes 等 OpenAI 兼容后端）下，只要在分类时输出
``missing_capability``、或在需要并行时调用 ``delegate``，同样两条链路都成立。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许脚本直接运行：补 examples/、src/ 与仓库根（后者用于 shell.employee）。
_EXAMPLES = Path(__file__).resolve().parents[1]
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _p in (str(_EXAMPLES), str(_SRC), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shell.employee import attach_delegate  # noqa: E402

from host_j_actual import capabilities as cap  # noqa: E402
from host_j_actual.backend import DEMO_MONTH, ActualBackend, FakeActualBackend  # noqa: E402
from yai_core import AgentCore, ModelResponse, ToolCallRequest  # noqa: E402
from yai_core.channels.collect import CollectChannel  # noqa: E402
from yai_core.policy import AllowlistPolicy  # noqa: E402

DEFAULT_TASK = (
    "帮我做 2026 年 9 月的对账：把未分类的交易按收款人归类，"
    "再核对各类预算有没有超支，最后给一份月度对账小结。"
)
# 措辞刻意含规则路由的动作词（列出/统计），使无 LLM 的离线规则路由也判定为
# react（use_tools=True），工具结果才会以 role=tool 回灌、模型据此收尾。
DEFAULT_DELEGATE_TASK = (
    "请并行处理两块只读汇总并列出结果：第一块列出全部账户与当前余额，"
    "第二块统计本月对账概览（未分类笔数、支出、超支分类），最后由你汇总。"
)

# 固定的未分类交易 → 建议分类（与 FakeActualBackend 的种子数据一致）。
_SEED_ASSIGNMENTS = [
    {"tx_id": "t3", "category_id": "c-food"},
    {"tx_id": "t5", "category_id": "c-grocery"},
    {"tx_id": "t6", "category_id": "c-transit"},
]


class ReconcileModel:
    """发现+授权链路的离线确定性模型（计数驱动，单线程顺序可靠）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier: str = "standard"):
        self.calls += 1
        if self.calls == 1:
            # 路由分类调用：判定现有工具无法自动归类，报告缺失能力。
            return ModelResponse(
                content=(
                    '{"strategy":"react","tier":"standard",'
                    '"reason":"月底对账需要把未分类交易自动归类，宿主无此能力",'
                    '"missing_capability":"交易自动分类建议能力"}'
                )
            )
        if self.calls == 2:
            # 先拉本月交易，定位未分类项（只读，白名单自动放行）。
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="r1",
                        name="list_recent_transactions",
                        arguments={"month": DEMO_MONTH},
                    )
                ],
            )
        if self.calls == 3:
            # suggest_category 已在本轮被发现、注册；先用它验证一条建议。
            # 该工具不在只读白名单，部分审批档下触发 PERMISSION_ASKED。
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="r2",
                        name="suggest_category",
                        arguments={"payee": "星巴克", "note": ""},
                    )
                ],
            )
        if self.calls == 4:
            # 批量补分类（写操作，再次走授权闸）。
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="r3",
                        name="categorize_transactions",
                        arguments={"assignments": _SEED_ASSIGNMENTS},
                    )
                ],
            )
        if self.calls == 5:
            # 核对预算，找出超支分类（只读）。
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="r4",
                        name="get_month_budget",
                        arguments={"month": DEMO_MONTH},
                    )
                ],
            )
        return ModelResponse(
            content=(
                "9 月对账完成：3 笔未分类交易已按收款人归类"
                "（星巴克→餐饮、盒马鲜生→食材杂货、滴滴出行→交通）。"
                "预算核对发现娱乐分类预算 100 元、实际 128 元，超支 28 元；"
                "餐饮、食材杂货、交通均在预算内。建议下月压缩娱乐支出。"
            )
        )


class _DenyReconcileModel:
    """授权被拒时的短链路模型：发现的新工具未获批，即诚实停止、不做写操作。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier: str = "standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content=(
                    '{"strategy":"react","tier":"standard",'
                    '"reason":"需要自动分类建议",'
                    '"missing_capability":"交易自动分类建议能力"}'
                )
            )
        if self.calls == 2:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="d1",
                        name="suggest_category",
                        arguments={"payee": "星巴克", "note": ""},
                    )
                ],
            )
        return ModelResponse(
            content="自动分类工具未获授权，已停止改账，未分类交易保持原样，请人工处理。"
        )


class DelegatingReconcileModel:
    """委派链路的无状态模型：根与子员工共用，只看消息、不记调用次数。

    - 子员工（user 目标以"子任务·"开头）：白名单里只有一个只读工具，首次直接
      调它，拿到工具结果后输出一句话小结。
    - 根员工：首次调用 ``delegate`` 派两个只读子员工；拿到回流结果后输出汇总。

    因为判定只依赖各自独立的消息列表，两个子员工在并行 gather 下交错调用也
    不会串状态。
    """

    WORKER_GOAL_ACCOUNTS = "子任务·列出全部账户与当前余额，只做只读汇总"
    WORKER_GOAL_OVERVIEW = "子任务·统计本月对账概览（未分类笔数、支出、超预算分类），只做只读汇总"

    @staticmethod
    def _field(message, key: str):
        """消息可能是 ChatMessage 对象，也可能是 OpenAI 形态的 dict，两者兼容。"""
        if isinstance(message, dict):
            return message.get(key)
        return getattr(message, key, None)

    async def achat(self, messages, tools=None, *, tier: str = "standard"):
        last = messages[-1] if messages else None
        last_role = self._field(last, "role")
        goal = " ".join(
            str(self._field(m, "content") or "")
            for m in messages
            if self._field(m, "role") == "user"
        )
        is_worker = goal.startswith("子任务·")

        if is_worker:
            if last_role == "tool":
                return ModelResponse(content="只读子任务完成，结果已回流给根员工。")
            if "账户" in goal:
                return ModelResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(id="w-acc", name="list_accounts", arguments={})
                    ],
                )
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="w-ov", name="reconcile_overview",
                        arguments={"month": DEMO_MONTH},
                    )
                ],
            )

        # 根员工
        if last_role == "tool":
            return ModelResponse(
                content=(
                    "两个只读子员工已并行完成：账户与余额、本月对账概览均已回流。"
                    "未分类笔数与超预算分类见概览，后续补分类等写操作由我（根员工）"
                    "在获得授权后统一处理。"
                )
            )
        return ModelResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="del",
                    name="delegate",
                    arguments={
                        "tasks": [
                            {"goal": self.WORKER_GOAL_ACCOUNTS, "tools": ["list_accounts"]},
                            {
                                "goal": self.WORKER_GOAL_OVERVIEW,
                                "tools": ["reconcile_overview"],
                            },
                        ]
                    },
                )
            ],
        )


def build_discovery_core(
    backend: ActualBackend | None = None,
    *,
    auto_confirm: bool = True,
    deny: bool = False,
) -> AgentCore:
    """发现+授权链路：部分审批档（读工具白名单放行，其余询问）。

    ``auto_confirm`` 控制离线通道对授权询问的回答（True=批准，False=拒绝）；
    ``deny`` 用更短的拒绝链路模型，便于测试"未获批即不改账"。
    """
    backend = backend or FakeActualBackend()
    tools = cap.make_tools(backend)
    model = _DenyReconcileModel() if deny else ReconcileModel()
    core = AgentCore(
        model,
        channel=CollectChannel(auto_confirm=auto_confirm),
        policy=AllowlistPolicy(cap.READ_TOOL_NAMES, mode="auto"),
        llm_router=True,
        discovery=cap.build_catalog(tools),
    )
    core.register_tools(cap.default_tool_specs(tools))
    return core


def build_delegate_core(backend: ActualBackend | None = None) -> AgentCore:
    """委派链路：根默认全放行（本地演示），挂载 delegate 工具。"""
    backend = backend or FakeActualBackend()
    tools = cap.make_tools(backend)
    core = AgentCore(DelegatingReconcileModel(), auto_approve_tools=True)
    core.register_tools(cap.default_tool_specs(tools))
    attach_delegate(core)
    return core
