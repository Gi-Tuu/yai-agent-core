"""离线演示脚本：用一串按时间轴推送的事件，驱动浮窗演出完整自适应过程。

不依赖 yai_core、不依赖 API Key/网络，用于：
- 无 Key 环境核对浮窗全部动效；
- 录屏时的稳定可重复开场。

事件为轻量 :class:`DemoEvent`（带 ``type`` 字符串与 ``data``），
:class:`shell.session.ShellSession` 会把它们原样推入 SSE。
真实模式下这些事件由内核产生、字段一致。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator


class DemoEvent:
    """最小事件结构：type + data，兼容会话的事件读取。"""

    def __init__(self, event_type: str, **data: object) -> None:
        self.type = event_type
        self.data = data


_AVAILABLE = ["list_customers", "list_orders", "sum_amount", "add_followup"]

#: (距上一事件的延迟秒数, 事件)。
SCRIPT: list[tuple[float, DemoEvent]] = [
    (
        0.4,
        DemoEvent(
            "strategy_selected",
            strategy="react",
            source="llm",
            reason="任务需要操作宿主数据并可能补齐新能力",
            tier="standard",
        ),
    ),
    (
        0.7,
        DemoEvent(
            "model_message",
            text="我先核对现有能力，再处理这笔含税报价。",
        ),
    ),
    (
        0.8,
        DemoEvent(
            "capability_missing",
            task="帮我算这笔订单的含税金额并归档",
            missing="含税金额计算（按税率计算税后报价）",
            available_tools=_AVAILABLE,
            strategy="react",
        ),
    ),
    (
        1.0,
        DemoEvent(
            "permission_asked",
            tool="request_capability",
            arguments={"missing": "含税金额计算", "source": "static"},
        ),
    ),
    # 演示模式自动"批准"：真实模式此处会挂起，等待用户在浮窗点批准。
    (
        2.6,
        DemoEvent(
            "tool_discovered",
            missing="含税金额计算",
            source="static",
            registered=["calc_with_tax"],
        ),
    ),
    (
        0.5,
        DemoEvent(
            "tool_call",
            tool="calc_with_tax",
            arguments={"amount": 10000, "tax_rate": 0.13},
        ),
    ),
    (
        1.0,
        DemoEvent(
            "tool_result",
            tool="calc_with_tax",
            ok=True,
            preview='{"amount": 10000, "with_tax": 11300}',
        ),
    ),
    (
        0.7,
        DemoEvent(
            "tool_composed",
            tool="tax_quote_brief",
            steps=["calc_with_tax", "add_followup"],
            arguments={"amount": 10000, "tax_rate": 0.13},
        ),
    ),
    (
        1.2,
        DemoEvent(
            "tool_result",
            tool="tax_quote_brief",
            ok=True,
            preview='{"steps": ["calc_with_tax", "add_followup"]}',
        ),
    ),
    (
        0.7,
        DemoEvent(
            "model_message",
            text="已按 13% 税率算出含税金额 11300 元，并归档为跟进记录。",
        ),
    ),
    (
        0.9,
        DemoEvent(
            "done",
            strategy="react",
            final_text="含税金额 11300 元，已归档。",
        ),
    ),
]


#: 并行委派场景：根把两个独立只读子目标派给子员工，结果只回流根、根对用户负责。
SCRIPT_DELEGATE: list[tuple[float, DemoEvent]] = [
    (
        0.4,
        DemoEvent(
            "strategy_selected",
            strategy="react",
            source="llm",
            reason="任务含两个彼此独立的只读子目标，适合并行委派",
            tier="standard",
        ),
    ),
    (
        0.7,
        DemoEvent(
            "model_message",
            text="这是两个独立的只读任务，我并行派两个子员工处理，再汇总给你。",
        ),
    ),
    (
        0.9,
        DemoEvent(
            "tool_call",
            tool="delegate",
            arguments={
                "tasks": [
                    {"goal": "汇总本月订单总额", "tools": ["list_orders", "sum_amount"]},
                    {"goal": "列出今天该跟进的客户", "tools": ["customers_due_followup"]},
                ]
            },
        ),
    ),
    # 子员工不直接对话、结果只回流根：等待后由 delegate 一次性返回汇总。
    (
        2.4,
        DemoEvent(
            "tool_result",
            tool="delegate",
            ok=True,
            preview=(
                '{"results": ['
                '{"goal": "汇总本月订单总额", "ok": true, '
                '"result": "本月订单合计 38700 元"}, '
                '{"goal": "列出今天该跟进的客户", "ok": true, '
                '"result": "2 位客户待跟进：华南智造、北辰科技"}]}'
            ),
        ),
    ),
    (
        0.8,
        DemoEvent(
            "model_message",
            text="本月订单合计 38700 元；今天有 2 位客户待跟进：华南智造、北辰科技。",
        ),
    ),
    (
        0.9,
        DemoEvent(
            "done",
            strategy="react",
            final_text="订单合计 38700 元；2 位客户待跟进。",
        ),
    ),
]


def _is_delegate_task(task: str) -> bool:
    """委派类任务按关键词识别，切换到并行委派脚本。"""
    return ("委派" in task) or ("并行" in task) or ("子员工" in task)


async def demo_stream(task: str, channel: object) -> AsyncIterator[DemoEvent]:
    """按时间轴产出演示事件：委派类任务走并行委派脚本，其余走能力发现脚本。"""
    script = SCRIPT_DELEGATE if _is_delegate_task(task) else SCRIPT
    for delay, event in script:
        await asyncio.sleep(delay)
        yield event
