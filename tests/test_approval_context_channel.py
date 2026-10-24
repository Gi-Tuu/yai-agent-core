"""审批上下文怎么递给通道：卡片弹出来的那一刻，事件还在半路上。

钉住的语义：

- **时序**：`PERMISSION_ASKED` 由执行器收集、`AgentLoop` 统一 yield，而
  `authorize_tool_call` 在**返回之前**就要拿到确认结果——所以界面不能靠事件读
  `risk/scope/target`，只能随 `confirm()` 一起递（下面第三条测试把这个"为什么"钉住）。
- **能力旗标，不是探签名**：只有声明 `yai_approval_context_aware` 的通道收到第三个参数；
  宿主写的通道可能有 `**kwargs`，探签名会猜错，猜错的那一次就是审批人少看一个字段。
- **只送描述性字段**：`arguments` 仍是按声明脱敏的对外副本，上下文里没有未脱敏参数值。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from yai_core import GateDecision, ToolGovernance, build_composite_spec, build_spec
from yai_core.channels.cli import CliChannel
from yai_core.channels.collect import CollectChannel
from yai_core.policy.authorize import authorize_tool_call
from yai_core.spi.policy import PermissionDecision
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import AgentEvent, EventType

# 假参数值从环境注入（离线占位，环境变量不存在时 setdefault 落一个占位串）：
# 本测试验证"声明了 sensitive 的字段一律脱敏"，与值是否长得像密钥无关——
# 脱敏契约本来就是不按字段名/值形状猜。
_ARGS = {
    "amount": 2000,
    "api_key": os.environ.setdefault(
        "YAI_TEST_REDACTION_KEY", "offline-credential-placeholder"
    ),
}


class _Allow:
    async def check(self, tool_name: str, arguments: dict) -> PermissionDecision:
        return PermissionDecision.ALLOW


class _LegacyChannel:
    """老通道：签名里就没有第三个参数。内核不许因为"升级了"把它叫崩。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.events: list[AgentEvent] = []

    async def emit(self, event: AgentEvent) -> None:
        self.events.append(event)

    async def ask(self, question: str) -> str:
        return ""

    async def confirm(self, tool_name: str, arguments: dict) -> bool:
        self.calls.append((tool_name, arguments))
        return True


class _ContextChannel:
    yai_approval_context_aware = True

    def __init__(self, *, approve: bool = True) -> None:
        self.approve = approve
        self.contexts: list[dict[str, Any] | None] = []
        self.shown: list[dict] = []
        self.events: list[AgentEvent] = []

    async def emit(self, event: AgentEvent) -> None:
        self.events.append(event)

    async def ask(self, question: str) -> str:
        return ""

    async def confirm(
        self, tool_name: str, arguments: dict, context: dict | None = None
    ) -> bool:
        self.contexts.append(context)
        self.shown.append(dict(arguments))
        return self.approve


def _run(
    channel,
    *,
    policy: Any = None,
    require: bool = False,
    reason: str = "",
    prompt: str = "",
    risk: str = "",
    scope: str = "",
    target: str = "",
    sensitive: tuple[str, ...] = ("api_key",),
) -> tuple:
    return asyncio.run(
        authorize_tool_call(
            policy or _Allow(),
            channel,
            "refund",
            dict(_ARGS),
            sensitive_paths=sensitive,
            require_approval=require,
            approval_reason=reason,
            approval_prompt=prompt,
            risk=risk,
            scope=scope,
            target=target,
        )
    )


# --- 能力旗标 -----------------------------------------------------------------


def test_channel_without_the_flag_never_sees_a_third_argument() -> None:
    channel = _LegacyChannel()
    _run(channel)  # 策略 ALLOW 且无审批要求：走不到 confirm
    assert channel.calls == []

    events, approved, _ = _run(channel, require=True, reason="该能力要求每次调用都问人")
    assert approved is True
    assert [len(call) for call in channel.calls] == [2], "老通道只该收到两个参数"
    assert events[0].type == EventType.PERMISSION_ASKED


def test_collect_channel_keeps_its_two_argument_signature() -> None:
    """内置的收集型通道**故意不带旗标**：批处理与测试要的就是"自动点头"，不该被塞新参数。

    它走的是 `isinstance` 之外的另一条合法路径——`getattr` 取不到旗标 → 两个参数。
    """
    collector = CollectChannel()
    events, approved, _ = _run(collector, require=True, reason="要问人", risk="high")
    assert approved is True
    asked = next(e for e in events if e.type == EventType.PERMISSION_ASKED)
    assert asked.data["reason"] == "要问人"


def test_prompt_key_in_the_context_is_present_only_when_there_really_is_one() -> None:
    """`prompt` 这个键的有无本身就是信息：没写模板就别给界面一个空串去渲染一行空白。"""
    with_prompt = _ContextChannel()
    _run(with_prompt, require=True, reason="要问人", prompt="退款 {amount} 元，批吗？")
    assert with_prompt.contexts[0]["prompt"] == "退款 2000 元，批吗？"

    without_prompt = _ContextChannel()
    _run(without_prompt, require=True, reason="要问人")
    assert "prompt" not in without_prompt.contexts[0]


def test_declaring_the_flag_is_what_buys_the_context() -> None:
    channel = _ContextChannel()
    _run(channel)
    assert channel.contexts == [], "没到 ASK 档就不该被问"

    _run(channel, require=True, reason="函数闸要问人", risk="high")
    assert channel.contexts == [
        {
            "tool": "refund",
            "approval_required": True,  # 界面标题靠它区分"参数级审批"与"新能力授权"
            "reason": "函数闸要问人",
            "risk": "high",
        }
    ], "没要求的字段（prompt / scope / target）不许凭空出现在上下文里"


# --- 内容边界 ----------------------------------------------------------------


def test_plain_policy_ask_gets_a_context_without_approval_fields() -> None:
    """策略自己 ASK（没有契约/原生审批）时，上下文里只有工具名与描述性字段。"""
    class _Ask:
        async def check(self, tool_name: str, arguments: dict) -> PermissionDecision:
            return PermissionDecision.ASK

    channel = _ContextChannel()
    events, approved, _ = _run(channel, policy=_Ask())
    assert approved is True
    assert channel.contexts == [{"tool": "refund"}]
    assert "approval_required" not in events[0].data


def test_context_carries_descriptors_only_never_unmasked_arguments() -> None:
    channel = _ContextChannel()
    _run(channel, require=True, reason="该能力要求每次调用都人工确认", risk="high")
    context = channel.contexts[0]
    assert "arguments" not in context, "参数走第二个位置，那份必须是脱敏副本"
    assert "[redacted" in str(channel.shown[0]["api_key"])
    assert "sk-secret" not in str(channel.shown[0])
    assert "sk-secret" not in str(context)


def test_declined_approval_shows_the_same_context_the_event_records() -> None:
    channel = _ContextChannel(approve=False)
    events, approved, text = _run(
        channel, require=True, reason="函数闸要问人", target="notes/a.txt"
    )
    assert approved is False
    assert "拒绝执行" in text
    assert events[0].data["reason"] == "函数闸要问人"
    assert channel.contexts[0]["target"] == "notes/a.txt"


# --- 时序：卡片先弹，事件后到 -------------------------------------------------


def test_descriptors_travel_with_the_call_because_the_event_comes_later() -> None:
    """confirm 被调用时，`PERMISSION_ASKED` 还**没**交给通道。

    这条不是刁钻：它是"为什么非要给通道加第三个参数"的唯一证据。若哪天有人把界面改成
    从事件流里读落点，卡片拿到的就是空值——看着没坏，其实审批人什么也没多看见。
    """
    channel = _ContextChannel()
    seen: dict[str, Any] = {}

    async def spy(tool_name: str, arguments: dict, context: dict | None = None) -> bool:
        seen["events_so_far"] = len(channel.events)
        seen["context"] = context
        return True

    channel.confirm = spy  # type: ignore[method-assign]
    _run(
        channel,
        require=True,
        reason="命中审批条件",
        prompt="退款 {amount} 元，批吗？",
        risk="high",
        scope="notes/",
        target="notes/a.txt",
    )
    assert seen["events_so_far"] == 0, "事件在 confirm 之后才投递，卡片那时读不到它"
    assert seen["context"]["target"] == "notes/a.txt"
    assert seen["context"]["prompt"] == "退款 2000 元，批吗？"


# --- 执行器那条路（端到端） ---------------------------------------------------


def _gate(**fields: Any):
    def gate(arguments: dict, subject: Any) -> GateDecision:
        return GateDecision(require_approval=True, **fields)

    return gate


def test_executor_routes_the_context_end_to_end() -> None:
    """从执行器进来的那条路也要带上下文，否则界面只在单测里好看。"""

    def create_file(path: str) -> dict:
        """建文件。"""
        return {"created": path}

    channel = _ContextChannel()
    spec = build_spec(create_file)
    spec.governance = ToolGovernance(
        gate=_gate(reason="作用域外的写", scope="notes/", target="secret/x.txt")
    )
    registry = ToolRegistry()
    registry.register(spec)
    executor = ToolExecutor(registry, _Allow(), channel)
    events, ok, _ = asyncio.run(executor.execute("create_file", {"path": "secret/x.txt"}))
    assert ok is True
    assert channel.contexts[0]["target"] == "secret/x.txt"
    assert channel.contexts[0]["scope"] == "notes/"
    asked = next(e for e in events if e.type == EventType.PERMISSION_ASKED)
    assert asked.data["target"] == "secret/x.txt"


# --- 内置通道真的把上下文用起来了 --------------------------------------------


def test_composite_path_routes_the_context_through_the_same_channel_rule() -> None:
    """组合工具的确认卡是执行器自己发的（不走 `authorize_tool_call`），口径必须一模一样。

    少了这条，同一个宿主在"普通工具"与"组合工具"两条路上会看到两种卡片——
    而组合那条恰好是"批准了整条流程"这种更该说清楚的情况。
    """

    def add_one(amount: int) -> dict:
        """加一。"""
        return {"value": amount + 1}

    def double(value: int) -> dict:
        """翻倍。"""
        return {"doubled": value * 2}

    channel = _ContextChannel()
    registry = ToolRegistry()
    registry.register(build_spec(add_one))
    registry.register(build_spec(double))
    composite = build_composite_spec(
        "flow",
        "加一再翻倍",
        [
            {"tool": "add_one", "args": {"amount": "{{$input.amount}}"}},
            {"tool": "double", "args": {"value": "{{$steps.0.value}}"}},
        ],
    )
    composite.governance = ToolGovernance(
        gate=_gate(reason="整条流程要确认", risk="high", scope="flow/", target="add_one→double")
    )
    registry.register(composite)

    events, ok, _ = asyncio.run(
        ToolExecutor(registry, _Allow(), channel).execute("flow", {"amount": 1})
    )
    assert ok is True
    assert channel.contexts[0] == {
        "tool": "flow",
        "risk": "high",
        "scope": "flow/",
        "target": "add_one→double",
        "approval_required": True,
        "reason": "整条流程要确认",
    }
    asked = events[0]
    assert asked.type == EventType.PERMISSION_ASKED and asked.data["target"] == "add_one→double"
    # 每一步仍各自走完整权限链路：内部工具的调用事件都要在。
    assert [e.data["tool"] for e in events if e.type == EventType.TOOL_CALL] == [
        "add_one",
        "double",
    ]


def test_cli_channel_uses_the_context_instead_of_only_the_argument_string(monkeypatch) -> None:
    asked: list[str] = []

    def fake_input(prompt: str = "") -> str:
        asked.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)
    channel = CliChannel()
    assert channel.yai_approval_context_aware is True
    approved = asyncio.run(
        channel.confirm(
            "file_create",
            {"path": "notes/a.txt"},
            {"reason": "命中审批条件", "target": "notes/a.txt", "risk": "high"},
        )
    )
    assert approved is True
    assert "命中审批条件" in asked[0] and "notes/a.txt" in asked[0]
    # 第三个参数是可选的：老口径（只有工具名 + 参数）照样能问。
    assert asyncio.run(channel.confirm("file_read", {"path": "a.txt"})) is True
