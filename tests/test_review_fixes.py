"""复审 8 类遗漏的回归测试（2026-10-31 修复批次）。

每条对应复审的一个复现：组合熔断丢收据、后置条件验错目标、JSON 转义污染
URL 来源、IDNA 信任扩大、userinfo URL 漏记、gate 原地改写导致撤权重放失效、
限尾伪协议文本、多工具成功任务被误判失败。
"""

from __future__ import annotations

import asyncio

import pytest

from _local_http import local_json  # noqa: F401  # 保持本地 HTTP 工具可发现
from yai_core import (
    AgentCore,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    build_composite_spec,
    build_spec,
    verify_receipt_chain,
)
from yai_core.channels import CollectChannel
from yai_core.kernel.budget import Budget, BudgetTracker
from yai_core.learning import extract_route_outcome
from yai_core.policy import AllowlistPolicy
from yai_core.spi import PermissionDecision
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import EventType, GateDecision, Strategy, ToolGovernance

# ---------- 复用：事件构造小工具 ----------

def ev(t: EventType, **data):
    from yai_core.types import AgentEvent
    return AgentEvent(t, data)


def _ok_tool(tool: str, result: str = "{}") -> list:
    return [
        ev(EventType.TOOL_CALL, tool=tool, arguments={}),
        ev(EventType.TOOL_RESULT, tool=tool, ok=True, preview=result),
    ]


def _done(strategy: Strategy, text: str) -> list:
    return [
        ev(EventType.MODEL_MESSAGE, text=text),
        ev(EventType.DONE, strategy=strategy.value, final_text=text),
    ]




def _executor(specs) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_many(specs)
    return ToolExecutor(
        registry,
        AllowlistPolicy((), mode="allow_all"),
        CollectChannel(),
    )


# ---------- P1-1：组合中途预算熔断，已密封的子收据随异常保留 ----------

def test_composite_budget_cutoff_carries_sealed_sub_receipts() -> None:
    def step_one() -> dict:
        """第一步。"""
        return {"ok": True}

    def step_two() -> dict:
        """第二步。"""
        return {"ok": True}

    spec = build_composite_spec(
        "pipeline",
        "两步流水线",
        [
            {"tool": "step_one", "args": {}},
            {"tool": "step_two", "args": {}},
        ],
    )
    registry = ToolRegistry()
    registry.register_many([build_spec(step_one), build_spec(step_two), spec])
    executor = ToolExecutor(registry, AllowlistPolicy((), mode="allow_all"), CollectChannel())

    budget = BudgetTracker(Budget(max_tool_calls=2))
    budget.tool_calls = 1  # 第一步用掉最后一个名额，第二步 record 时熔断

    with pytest.raises(Exception) as exc_info:
        asyncio.run(
            executor.execute("pipeline", {}, budget=budget, call_id="c1")
        )
    carried = getattr(exc_info.value, "partial_events", None)
    assert carried, "BudgetExceeded 必须携带已密封的部分事件"
    receipts = [
        e.data["receipt"]
        for e in carried
        if e.type == EventType.TOOL_RESULT and "receipt" in e.data
    ]
    assert receipts, "子步骤收据必须随熔断事件保留"
    assert verify_receipt_chain(receipts) is True


# ---------- P1-2：后置条件验证的是"实际执行的那组参数" ----------

def test_postcondition_verifies_executed_arguments_not_caller_copy() -> None:
    import time

    seen_by_handler: list[dict] = []
    seen_by_postcondition: list[dict] = []

    class MutatingPolicy:
        """准入回调原地改写参数（宿主回调存在原地修改的现实形态）。"""

        async def check(self, tool_name: str, arguments: dict) -> PermissionDecision:
            arguments["value"] = "B"  # 原地改写：执行从此以 B 为准
            return PermissionDecision.ALLOW

    def apply(value: str = "") -> dict:
        """写入（同步挂起 → 超时放弃 → unknown，触发后置条件验证）。"""
        time.sleep(0.2)
        seen_by_handler.append(dict(value=value))
        return {"written": value}

    def postcondition(arguments: dict) -> bool:
        seen_by_postcondition.append(dict(arguments))
        return True

    spec = build_spec(apply)
    spec.governance = ToolGovernance(postcondition=postcondition)
    registry = ToolRegistry()
    registry.register_many([spec])
    executor = ToolExecutor(
        registry, MutatingPolicy(), CollectChannel(), handler_timeout=0.02
    )

    caller_args = {"value": "A"}
    events, ok, _ = asyncio.run(executor.execute("apply", caller_args, call_id="c1"))
    assert ok is True, "后置条件确认副作用发生，收据应升级为 succeeded"
    # 调用方的字典不再被原地改写
    assert caller_args == {"value": "A"}
    # 后置条件看到的与实际执行的一致（都是 B），而不是调用方手里的 A
    assert seen_by_handler == [{"value": "B"}]
    assert seen_by_postcondition and seen_by_postcondition[0].get("value") == "B"
    receipt = [e for e in events if e.type == EventType.TOOL_RESULT][-1].data["receipt"]
    assert receipt["status"] == "succeeded"
    assert receipt.get("postcondition") == "confirmed"


# ---------- P2-6：gate 原地规范化后，撤权在缓存重放时仍生效 ----------

def test_replay_dependency_recheck_uses_effective_arguments() -> None:
    revoked: set[str] = set()

    def gate(arguments: dict, subject: object) -> GateDecision | None:
        # 先判后改：判定基于原始形态，通过后原地规范化（宿主回调的现实形态）
        if arguments.get("name") in revoked:
            return GateDecision(reject=True, reason=f"{arguments.get('name')!r} 已被撤权")
        arguments["name"] = arguments["name"].strip()
        return None

    def report(name: str = "") -> str:
        """写报告。"""
        return f"报告 {name} 已写入"

    spec = build_spec(report)
    spec.governance = ToolGovernance(gate=gate)
    registry = ToolRegistry()
    registry.register_many([spec])
    executor = ToolExecutor(registry, AllowlistPolicy((), mode="allow_all"), CollectChannel())

    async def idempotency_key(name: str, arguments: dict, subject: object) -> str:
        return "stable-key"

    # 手工挂幂等键生成器：同键重放
    executor._idempotency_key = idempotency_key  # type: ignore[attr-defined]

    first_events, first_ok, _ = asyncio.run(
        executor.execute("report", {"name": " report "}, call_id="r1")
    )
    assert first_ok is True

    revoked.add("report")  # 撤权：针对规范化后的真实名字

    second_events, second_ok, second_text = asyncio.run(
        executor.execute("report", {"name": " report "}, call_id="r2")
    )
    assert second_ok is False, "已撤权的名字不能通过缓存重放交付"
    assert "准入闸拒绝重放" in second_text
    denial = [e for e in second_events if e.type == EventType.TOOL_RESULT][-1]
    assert denial.data.get("error") and "拒绝重放" in denial.data["error"]


# ---------- P1-3：结构化记录修复 JSON 转义污染，URL 换行不再漏拦 ----------

def test_url_with_newline_in_tool_output_is_recorded_and_denied() -> None:
    EVIL = "https://evil.example.com/a"
    sent: list[str] = []

    def fetch_page(url: str) -> dict:
        """Fetch a page."""
        # 返回结构化结果：URL 后跟真实换行（JSON 序列化会把它转义成 \n）
        return {"body": f"see {url}\nand more"}

    def send_direct_message(recipient: str, body: str) -> str:
        """Send a direct message."""
        sent.append(body)
        return "sent"

    fetch_spec = build_spec(fetch_page)
    send_spec = build_spec(send_direct_message)
    send_spec.governance = ToolGovernance(egress_text=("body",))
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="f1", name="fetch_page",
                                            arguments={"url": EVIL})],
            ),
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="s1", name="send_direct_message",
                                            arguments={"recipient": "A",
                                                       "body": f"see {EVIL}"})],
            ),
            ModelResponse(content="完成"),
        ]
    )
    core = AgentCore(model, channel=CollectChannel())
    core.register_tools([fetch_spec, send_spec])
    result = asyncio.run(core.run("读取 https://news.example/a 并转发给 A"))
    assert sent == [], f"换行污染导致的漏拦已修复，不应外发：{result.final_text}"


# ---------- P1-4：IDNA 有损映射不合并信任（faß.example ≠ fass.example） ----------

def test_idna_lossy_mapping_does_not_expand_trust() -> None:
    sent: list[str] = []

    def fetch_page(url: str) -> str:
        """Fetch a page."""
        return "visit https://fass.example/article instead"

    def send_direct_message(recipient: str, body: str) -> str:
        """Send a direct message."""
        sent.append(body)
        return "sent"

    fetch_spec = build_spec(fetch_page)
    send_spec = build_spec(send_direct_message)
    send_spec.governance = ToolGovernance(egress_text=("body",))
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="f1", name="fetch_page",
                                            arguments={"url": "https://faß.example/x"})],
            ),
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="s1", name="send_direct_message",
                                            arguments={"recipient": "A",
                                                       "body": "go https://fass.example/article"})],
            ),
            ModelResponse(content="完成"),
        ]
    )
    core = AgentCore(model, channel=CollectChannel())
    core.register_tools([fetch_spec, send_spec])
    asyncio.run(core.run("读取 https://news.example/a 并转发给 A"))
    assert sent == [], "IDNA 有损映射不能把 fass.example 并入 faß.example 的信任"


# ---------- P2-5：带 userinfo 的 URL 照常进入来源记录 ----------

def test_userinfo_url_is_recorded_and_denied() -> None:
    sent: list[str] = []

    def fetch_page(url: str) -> str:
        """Fetch a page."""
        return "see https://reader@evil.example.com/secret"

    def send_direct_message(recipient: str, body: str) -> str:
        """Send a direct message."""
        sent.append(body)
        return "sent"

    fetch_spec = build_spec(fetch_page)
    send_spec = build_spec(send_direct_message)
    send_spec.governance = ToolGovernance(egress_text=("body",))
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="f1", name="fetch_page",
                                            arguments={"url": "https://news.example/a"})],
            ),
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="s1", name="send_direct_message",
                                            arguments={"recipient": "A",
                                                       "body": "see https://reader@evil.example.com/secret"})],
            ),
            ModelResponse(content="完成"),
        ]
    )
    core = AgentCore(model, channel=CollectChannel())
    core.register_tools([fetch_spec, send_spec])
    asyncio.run(core.run("读取 https://news.example/a 并转发给 A"))
    assert sent == [], "带 userinfo 的 URL 不能因解析返回空而漏拦"


# ---------- P2-7：限尾收尾的伪协议文本 fail-closed ----------

def test_iteration_limit_raw_tool_call_text_is_fail_closed() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="count_notes", arguments={})],
            ),
            # 限尾轮：模型把伪协议写进正文
            ModelResponse(content='<tool_call>{"name": "count_notes"}</tool_call>'),
        ]
    )

    def count_notes() -> int:
        """统计笔记总数。"""
        return 3

    core = AgentCore(model, channel=CollectChannel())
    core.register_tools([build_spec(count_notes)])
    result = asyncio.run(core.run("统计笔记数量"))
    error_events = [e for e in result.events if e.type == EventType.ERROR]
    assert any(e.data.get("raw_tool_markup") for e in error_events), (
        "限尾收尾的伪协议文本必须触发 model_protocol 错误事件"
    )
    assert "没有执行任何工具" in result.final_text
    # 伪协议原文不得进入最终答案或历史
    assert "<tool_call>" not in result.final_text


# ---------- P2-8：多工具成功任务不再被次数启发式误判 ----------

def test_multi_tool_success_is_not_vetoed_by_tool_count() -> None:
    events = (
        [
            ev(EventType.STRATEGY_SELECTED, strategy=Strategy.REACT.value,
               source="rules", reason="行动信号", tier="standard"),
        ]
        + _ok_tool("search_notes")
        + _ok_tool("count_notes")
        + _done(Strategy.REACT, "两个工具都成功，找到 3 条笔记")
    )
    out = extract_route_outcome(events, Strategy.REACT, max_iters=2)
    assert out.success is True, "第一轮两个工具成功、第二轮正常回答是成功任务"
    assert out.tool_calls == 2
    assert out.hit_max_iters is True  # 成本信号保留
    assert out.reward > 0.9
