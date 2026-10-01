"""离线演示脚本：按时间轴推送事件，驱动灵动岛演出完整自适应过程。

不依赖 API Key/网络，用于无 Key 环境核对动效与录屏。

关键设计：脚本里出现 ``permission_asked`` / ``clarify_requested`` 步骤时，
**不伪造事件**，而是真的调用通道（``channel.confirm`` / ``channel.ask``）挂起等待
用户在原生界面上点按钮——所以离线演示里的授权卡是真卡，拒绝也是真拒绝。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence


class DemoEvent:
    """最小事件结构：type + data，兼容专员运行时的读取。"""

    def __init__(self, event_type: str, **data: object) -> None:
        self.type = event_type
        self.data = data


#: (距上一事件的延迟秒数, 事件)。
_SALES_GAP = "含税金额计算（按税率计算税后报价）"

SCRIPT: Sequence[tuple[float, DemoEvent]] = [
    (0.4, DemoEvent("strategy_selected", strategy="react", source="llm",
                    reason="任务需要操作宿主数据并可能补齐新能力", tier="standard")),
    (0.7, DemoEvent("model_message", text="我先核对现有能力，再处理这笔含税报价。")),
    (0.8, DemoEvent("capability_missing", missing=_SALES_GAP, strategy="react")),
    # 这一步会真的挂起：等用户在原生面板点"允许/拒绝"
    (0.6, DemoEvent("permission_asked", tool="request_capability",
                    arguments={"missing": _SALES_GAP})),
    (1.6, DemoEvent("tool_discovered", missing=_SALES_GAP, source="static",
                    registered=["calc_with_tax"])),
    (0.5, DemoEvent("tool_call", tool="calc_with_tax",
                    arguments={"amount": 10000, "tax_rate": 0.13})),
    (0.9, DemoEvent("tool_result", tool="calc_with_tax", ok=True,
                    preview='{"amount": 10000, "with_tax": 11300}')),
    (0.6, DemoEvent("tool_composed", tool="tax_quote_brief",
                    steps=["calc_with_tax", "add_followup"])),
    (0.8, DemoEvent("model_message",
                    text="已按 13% 税率算出含税金额 11300 元，并归档为跟进记录。")),
    (0.6, DemoEvent("done", strategy="react", final_text="含税金额 11300 元，已归档。")),
]

#: 并行委派：根把两个独立只读子目标派给子员工，结果只回流根。
SCRIPT_DELEGATE: Sequence[tuple[float, DemoEvent]] = [
    (0.4, DemoEvent("strategy_selected", strategy="react", source="llm",
                    reason="任务含两个彼此独立的只读子目标，适合并行委派")),
    (0.7, DemoEvent("model_message",
                    text="这是两个独立的只读任务，我并行派两个子员工处理，再汇总给你。")),
    (0.9, DemoEvent("tool_call", tool="delegate", arguments={"tasks": [
        {"goal": "汇总本月订单总额", "tools": ["list_orders", "sum_amount"]},
        {"goal": "列出今天该跟进的客户", "tools": ["customers_due_followup"]},
    ]})),
    (2.4, DemoEvent("tool_result", tool="delegate", ok=True, preview=(
        '{"results": [{"goal": "汇总本月订单总额", "ok": true, "result": "38700 元"}, '
        '{"goal": "列出今天该跟进的客户", "ok": true, "result": "华南智造、北辰科技"}]}'))),
    (0.8, DemoEvent("model_message",
                    text="本月订单合计 38700 元；今天 2 位客户待跟进：华南智造、北辰科技。")),
    (0.6, DemoEvent("done", strategy="react",
                    final_text="订单合计 38700 元；2 位客户待跟进。")),
]

#: 仓库专员：低库存 → 补货写操作要授权。
SCRIPT_WAREHOUSE: Sequence[tuple[float, DemoEvent]] = [
    (0.4, DemoEvent("strategy_selected", strategy="react", source="rules",
                    reason="查询 + 补货两步，写操作需授权")),
    (0.6, DemoEvent("tool_call", tool="list_products")),
    (0.8, DemoEvent("tool_result", tool="list_products", ok=True,
                    preview='["螺丝", "轴承", "线缆"]')),
    (0.5, DemoEvent("tool_call", tool="get_stock", arguments={"name": "轴承"})),
    (0.8, DemoEvent("tool_result", tool="get_stock", ok=True,
                    preview='{"name": "轴承", "stock": 8}')),
    (0.5, DemoEvent("model_message", text="轴承只剩 8 件，低于安全线，我建议补 40 件。")),
    (0.6, DemoEvent("permission_asked", tool="restock",
                    arguments={"name": "轴承", "qty": 40})),
    (0.9, DemoEvent("tool_result", tool="restock", ok=True,
                    preview='{"name": "轴承", "stock": 48}')),
    (0.6, DemoEvent("done", strategy="react", final_text="轴承已补至 48 件。")),
]

#: 笔记专员：回忆不到 → 反问澄清。
SCRIPT_NOTES: Sequence[tuple[float, DemoEvent]] = [
    (0.4, DemoEvent("strategy_selected", strategy="clarify", source="llm",
                    reason="“上次那个”指向不明，先澄清")),
    (0.6, DemoEvent("tool_call", tool="search_notes", arguments={"keyword": "上次"})),
    (0.9, DemoEvent("tool_result", tool="search_notes", ok=True, preview="[]")),
    # 这一步会真的挂起：等用户在原生面板输入一句回答
    (0.5, DemoEvent("clarify_requested", question="“上次那个”指哪一份？周报还是复盘？")),
    (1.0, DemoEvent("tool_call", tool="recall_note", arguments={"tag": "复盘"})),
    (0.9, DemoEvent("tool_result", tool="recall_note", ok=True,
                    preview='{"title": "9 月复盘", "items": 4}')),
    (0.6, DemoEvent("done", strategy="clarify",
                    final_text="找到《9 月复盘》，共 4 条要点。")),
]

#: 陪伴专员：主动事件 + 现场组合工具。
SCRIPT_COMPANION: Sequence[tuple[float, DemoEvent]] = [
    (0.4, DemoEvent("strategy_selected", strategy="plan", source="llm",
                    reason="先记录心情，再回看近三天趋势")),
    (0.7, DemoEvent("tool_call", tool="log_mood", arguments={"level": 3})),
    (0.8, DemoEvent("tool_result", tool="log_mood", ok=True, preview='{"day": "10-01"}')),
    (0.6, DemoEvent("capability_missing", missing="近三天心情趋势汇总")),
    (0.9, DemoEvent("tool_composed", tool="mood_trend_3d",
                    steps=["recall_moods", "summarize_trend"])),
    (1.0, DemoEvent("tool_result", tool="mood_trend_3d", ok=True,
                    preview='{"trend": "回升", "days": 3}')),
    (0.7, DemoEvent("model_message", text="这三天心情在回升，明天要不要安排一次散步？")),
    (0.6, DemoEvent("done", strategy="plan", final_text="趋势回升，已给出明日建议。")),
]


def _is_delegate_task(task: str) -> bool:
    """委派类任务按关键词识别，切换到并行委派脚本。"""
    return ("委派" in task) or ("并行" in task) or ("子员工" in task)


def make_demo_stream(
    default: Sequence[tuple[float, DemoEvent]],
    alternatives: dict[str, Sequence[tuple[float, DemoEvent]]] | None = None,
):
    """把一份脚本变成事件流工厂：挂起步骤真的走通道。"""

    async def stream(task: str, channel) -> AsyncIterator[DemoEvent]:
        script = default
        for keyword, alt in (alternatives or {}).items():
            if keyword in task:
                script = alt
                break
        async for event in _replay(script, channel):
            yield event

    return stream


async def _replay(script, channel) -> AsyncIterator[DemoEvent]:
    """按时间轴产出事件；遇到挂起步骤改为真的向界面请求决策。"""
    for delay, event in script:
        await asyncio.sleep(delay)
        kind = event.type
        if kind == "permission_asked" and channel is not None:
            approved = await channel.confirm(
                str(event.data.get("tool", "")),
                dict(event.data.get("arguments", {})),
            )
            if not approved:
                yield DemoEvent("tool_result", tool=str(event.data.get("tool", "")),
                                ok=False, error="用户拒绝，未执行该操作")
                yield DemoEvent("done", final_text="已按你的决定收尾：该操作未执行。")
                return
            continue
        if kind == "clarify_requested" and channel is not None:
            answer = await channel.ask(str(event.data.get("question", "")))
            if not answer:
                yield DemoEvent("done", final_text="没有拿到澄清回答，先不猜测。")
                return
            continue
        yield event


demo_stream = make_demo_stream(
    SCRIPT, {"委派": SCRIPT_DELEGATE, "并行": SCRIPT_DELEGATE, "子员工": SCRIPT_DELEGATE}
)
