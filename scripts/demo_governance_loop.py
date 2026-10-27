"""一条命令跑完治理闭环：请求 → 工具调用 → 审批拦截 → 批准/拒绝 → 执行 → 审计 trace。

和 ``demo_approval_flow.py``（只演示闸门与审批）的分工：这个脚本走**完整 Agent 循环**，
模型发起工具调用、内核在执行前拦下高风险写操作、人批准后才真正执行，并把全过程写成
可回查的 JSONL trace——用来回答"这个内核到底怎么管住一次写操作"。

全程**离线、零 API Key**（脚本模型 + 临时目录），不触达任何真实支付或业务系统。

用法::

    python scripts/demo_governance_loop.py                 # 批准，执行并落 trace
    python scripts/demo_governance_loop.py --refuse        # 拒绝，验证零副作用
    python scripts/demo_governance_loop.py --trace out.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec  # noqa: E402
from yai_core.channels import CollectChannel  # noqa: E402
from yai_core.policy import AllowlistPolicy  # noqa: E402
from yai_core.types import (  # noqa: E402
    ApprovalPolicy,
    ApprovalRule,
    EventType,
    ToolGovernance,
)

#: 演示订单与金额：写操作会触发审批（> 100 元）。任务里带行动信号，路由会进工具循环。
DEMO_TASK = "调用退款接口，为订单 SO-1001 退款 199 元，金额我确认过"
DEMO_ORDER = "SO-1001"
DEMO_AMOUNT = 199


class ApprovalChannel(CollectChannel):
    """把"要不要批准"打到屏幕上，并按脚本回答；记录被问过的每一次。"""

    def __init__(self, approve: bool) -> None:
        super().__init__()
        self.approve = approve
        self.asked: list[tuple[str, dict]] = []

    async def confirm(self, tool_name: str, arguments: dict) -> bool:
        self.asked.append((tool_name, dict(arguments)))
        print(f"  ⏸  内核拦下：{tool_name} 需要人工批准")
        # 参数取值属于审计数据：只进 trace 文件，不打到终端（终端可能被日志采集）。
        print(f"     冻结参数：{len(arguments)} 项（完整取值见 trace）")
        print(f"     操作人回答：{'批准' if self.approve else '拒绝'}")
        return self.approve


class RefundModel:
    """离线脚本模型：第一轮发起退款工具调用，第二轮基于工具结果收尾。"""

    yai_multimodal = True

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier: str = "standard"):
        self.calls += 1
        if self.calls == 1 and tools:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="refund-1",
                        name="issue_refund",
                        arguments={"order_id": DEMO_ORDER, "amount": DEMO_AMOUNT},
                    )
                ],
            )
        last = next((m for m in reversed(messages) if m.get("role") == "tool"), None)
        observed = (last or {}).get("content", "")
        return ModelResponse(content=f"宿主返回：{observed[:200]}")


def build_core(approve: bool, ledger: list[dict]) -> tuple[AgentCore, ApprovalChannel]:
    """装配：一个真实姿态的宿主写操作 + 参数级审批 + 只在批准后执行。"""

    async def issue_refund(order_id: str, amount: int) -> dict:
        """为指定订单发起退款（真实宿主里会调用支付网关）。"""
        # 只有人批准后才会走到这里：拒绝时内核不会调用 handler。
        ledger.append({"order_id": order_id, "amount": amount})
        return {"order_id": order_id, "amount": amount, "status": "refunded"}

    spec = build_spec(issue_refund)
    spec.governance = ToolGovernance(
        approval=ApprovalPolicy(
            prompt="为订单 {order_id} 退款 {amount} 元，确认执行？",
            when=(ApprovalRule(param="amount", op=">", value=100, label="金额超限"),),
        )
    )
    channel = ApprovalChannel(approve)
    core = AgentCore(
        RefundModel(),
        channel=channel,
        policy=AllowlistPolicy((), mode="auto"),
        auto_approve_tools=False,
        llm_router=False,
    )
    core.register_tools([spec])
    return core, channel


def _describe(event) -> str | None:
    data = event.data
    kind = event.type
    if kind == EventType.STRATEGY_SELECTED:
        return f"策略：{data.get('strategy')}（来源 {data.get('source')}）"
    if kind == EventType.TOOL_CALL:
        return f"调用：{data.get('tool')}（参数取值见 trace）"
    if kind == EventType.PERMISSION_ASKED:
        return (
            f"审批拦截：{data.get('tool')} "
            f"（参数取值见 trace）"
        )
    if kind == EventType.TOOL_RESULT:
        detail = data.get("error") or data.get("preview")
        return f"结果：ok={data.get('ok')} {detail or ''}".rstrip()
    if kind == EventType.ERROR:
        return f"错误：{data.get('message') or data.get('error')}"
    if kind == EventType.DONE:
        return f"完成：{data.get('final_text') or ''}"
    return None


def write_trace(path: Path, events) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for seq, event in enumerate(events, 1):
            handle.write(
                json.dumps(
                    {"seq": seq, "type": event.type.value, "data": event.data},
                    ensure_ascii=False,
                )
                + "\n"
            )


async def run(approve: bool, trace: Path) -> dict:
    ledger: list[dict] = []
    core, channel = build_core(approve, ledger)
    try:
        result = await core.run(DEMO_TASK)
    finally:
        await core.aclose()

    events = list(result.events)
    print(f"【1】用户请求：{DEMO_TASK}")
    print("【2】事件时间线：")
    for event in events:
        line = _describe(event)
        if line:
            print(f"  · {line}")
    print("【3】结论：")
    print(f"  · 审批次数：{len(channel.asked)}（应为 1，且答案={'批准' if approve else '拒绝'}）")
    print(f"  · 宿主实际执行：{len(ledger)} 次（拒绝时必须是 0）")
    write_trace(trace, events)
    print(f"  · 审计 trace：{trace}")

    return {
        "approved": approve,
        "approval_count": len(channel.asked),
        "executed": len(ledger),
        "ledger": ledger,
        "trace": str(trace),
        "event_count": len(events),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="治理闭环演示（离线）")
    parser.add_argument("--refuse", action="store_true", help="在审批处拒绝，验证零副作用")
    parser.add_argument("--trace", type=Path, help="trace 落盘路径（默认写入临时目录）")
    parser.add_argument("--json", action="store_true", help="额外输出机器可读结果")
    args = parser.parse_args()

    if args.trace is not None:
        trace = args.trace
        payload = asyncio.run(run(not args.refuse, trace))
    else:
        trace = Path(tempfile.mkdtemp(prefix="yai-trace-")) / "trace.jsonl"
        payload = asyncio.run(run(not args.refuse, trace))

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    # 退出码即断言：拒绝时不许有副作用，批准时必须恰好执行一次。
    expected = 0 if args.refuse else 1
    return 0 if payload["executed"] == expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
