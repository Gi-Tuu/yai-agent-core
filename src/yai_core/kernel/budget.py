"""任务级预算硬上限与熔断：给 Agent 的花费/动作上一道"硬墙"。

内核零第三方依赖，本模块只用标准库。预算是**任务级**的：每次 ``AgentCore.run``
/ ``astream`` 开始时新建一个 :class:`BudgetTracker`，澄清反问的递归重路由共享同
一计数器（不能靠"多反问几次"绕过上限），任务结束即废弃，不跨任务累计。

三类硬上限（均可选，``None`` 表示不限制）：

- ``max_model_calls``：执行期模型调用次数（规划 / 工具聚焦 / ReAct 主轮 / 重规划
  / 收尾，经内核统一入口 ``_model_chat`` 的调用；**不含**路由分类与上下文压缩，
  这两类各有独立开关、频率有界）。与 ``DONE.model_calls`` 同口径。
- ``max_tool_calls``：实际经 :class:`ToolExecutor` 执行的业务工具次数（与
  ``TOOL_CALL`` 事件同口径）；内核 meta-tool（发现 / 创建 / 组合 / 重规划）的
  编排动作不计入。
- ``max_tokens``：执行期累计 token（prompt + completion）。仅当模型后端在
  :class:`~yai_core.types.ModelResponse` 中返回真实 usage 时才累计；后端不提供
 用量时该上限不生效（内核不臆造 token 数），此时请改用次数上限兜底。

熔断语义（"硬"而非"软提示"）：一旦达到任一上限，立即抛出 :class:`BudgetExceeded`，
内核不再发起任何新的模型或工具调用，发 ``BUDGET_EXCEEDED`` 事件后用固定诚实文案
收尾，并在 ``DONE`` 上标记 ``reason="budget_exceeded"`` 与预算快照。要按金额而非
token 限流，请在宿主侧包一层计费 ``ModelProvider``（各模型单价不同，内核不内置价目表）。
"""

from __future__ import annotations

from dataclasses import dataclass

from yai_core.types import ModelResponse

#: 预算维度标识，用于事件 data 与异常信息。
SCOPE_MODEL_CALLS = "model_calls"
SCOPE_TOOL_CALLS = "tool_calls"
SCOPE_TOKENS = "tokens"


class BudgetExceeded(Exception):
    """任务预算硬上限被触发。

    由 :class:`BudgetTracker` 在"再调一次就越界"时抛出，沿 Agent Loop 冒泡到
    统一入口后转成 ``BUDGET_EXCEEDED`` 事件体面收尾，不作为未处理异常穿透给宿主。
    """

    def __init__(self, scope: str, limit: int, used: int) -> None:
        self.scope = scope
        self.limit = limit
        self.used = used
        # 熔断时执行器已密封、但还没来得及外发的事件（含执行收据）。
        # 组合工具中途熔断时由 executor 挂载、AgentLoop 补发，保证收据链不缺环。
        self.partial_events: list = []
        super().__init__(
            f"任务预算上限已触发：{scope} 已用 {used}、上限 {limit}，已熔断停止"
        )


def _check_non_negative(value: object, name: str) -> int | None:
    """校验预算字段：None 放行，非负整数放行，其余抛 ValueError。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数或 None，得到 {value!r}")
    return value


@dataclass(frozen=True)
class Budget:
    """任务级预算规格（不可变）；三个字段均为 None 时表示不设上限。"""

    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        # frozen dataclass 仍可在 __post_init__ 用 object.__setattr__，这里只做校验。
        _check_non_negative(self.max_model_calls, "max_model_calls")
        _check_non_negative(self.max_tool_calls, "max_tool_calls")
        _check_non_negative(self.max_tokens, "max_tokens")

    @property
    def enabled(self) -> bool:
        """是否设置了任一上限（未设置时内核连跟踪器都不必接线）。"""
        return (
            self.max_model_calls is not None
            or self.max_tool_calls is not None
            or self.max_tokens is not None
        )

    def start(self) -> BudgetTracker:
        """为一次新任务创建一个从零计数的跟踪器。"""
        return BudgetTracker(self)


@dataclass
class BudgetTracker:
    """单个任务执行期的预算计数与熔断判定（非线程安全，依赖内核串行契约）。"""

    spec: Budget
    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    #: 后端是否至少返回过一次真实 usage（决定 token 上限是否"有牙"）。
    token_usage_seen: bool = False
    #: 已触发的熔断（None=未触发）；用于 DONE 标记 reason。
    exceeded: BudgetExceeded | None = None

    def _trip(self, exc: BudgetExceeded) -> None:
        self.exceeded = exc
        raise exc

    def before_model_call(self) -> None:
        """发起一次模型调用前检查：调用次数已达上限则立即熔断（本次不发起）。"""
        limit = self.spec.max_model_calls
        if limit is not None and self.model_calls >= limit:
            self._trip(BudgetExceeded(SCOPE_MODEL_CALLS, limit, self.model_calls))

    def record_model_call(self, response: ModelResponse | None) -> None:
        """一次模型调用返回后计数；若因此越过 token 上限立即熔断。

        调用失败（无 response）不计入成功次数，也不累计 token——预算防的是持续
        成功烧钱，失败重试由 :class:`~yai_core.llm.fallback.FallbackModelProvider`
        的退避/冷却另行约束。
        """
        self.model_calls += 1
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.token_usage_seen = True
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        total = int(getattr(usage, "total_tokens", 0) or 0)
        # 后端若没给 total，用**本次增量**兜底；用累计值会把前几次的 token
        # 反复加进来（10、30、60 而不是 10、20、30），提前触发预算熔断。
        self.total_tokens += total or (prompt + completion)
        limit = self.spec.max_tokens
        if limit is not None and self.total_tokens >= limit:
            self._trip(BudgetExceeded(SCOPE_TOKENS, limit, self.total_tokens))

    def before_tool_call(self) -> None:
        """执行一个业务工具前检查：工具次数已达上限则不执行、立即熔断。"""
        limit = self.spec.max_tool_calls
        if limit is not None and self.tool_calls >= limit:
            self._trip(BudgetExceeded(SCOPE_TOOL_CALLS, limit, self.tool_calls))

    def record_tool_call(self) -> None:
        """一个业务工具已交给执行器（无论成功失败）后计数。"""
        self.tool_calls += 1

    def snapshot(self) -> dict[str, object]:
        """导出可 JSON 序列化的预算用量快照（随 DONE / BUDGET_EXCEEDED 事件流出）。"""
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "token_usage_seen": self.token_usage_seen,
            "limits": {
                "max_model_calls": self.spec.max_model_calls,
                "max_tool_calls": self.spec.max_tool_calls,
                "max_tokens": self.spec.max_tokens,
            },
        }
