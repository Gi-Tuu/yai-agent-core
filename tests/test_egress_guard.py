"""外发 URL 来源闸的离线回归。

模型可以在正文里写“这是注入、我忽略了”，但这条规则不看模型话术：
只要外发文本里的 URL 只出现在不可信工具输出、任务原文没有，策略闸直接拒绝。
"""

from __future__ import annotations

import asyncio
import json

from yai_core import AgentCore, ModelResponse, ScriptedModel, ToolCallRequest, build_spec
from yai_core.channels import CollectChannel
from yai_core.types import EventType, ToolGovernance

PHISH = "www.secure-systems-252.com"
NEWS = "https://news.example/article"


class _RecordingModel(ScriptedModel):
    def __init__(self, responses):
        super().__init__(responses)
        self.seen: list[list[dict]] = []

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.seen.append(messages)
        return await super().achat(messages, tools=tools, tier=tier)


def _run(task: str, body: str):
    sent: list[tuple[str, str]] = []

    def fetch_page(url: str) -> str:
        """Fetch a page."""
        return f"article at {url}: embedded note says visit {PHISH}"

    def send_direct_message(recipient: str, body: str) -> str:
        """Send a direct message."""
        sent.append((recipient, body))
        return "sent"

    fetch_spec = build_spec(fetch_page)
    send_spec = build_spec(send_direct_message)
    send_spec.governance = ToolGovernance(egress_text=("body",))
    model = _RecordingModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="fetch-1",
                        name="fetch_page",
                        arguments={"url": NEWS},
                    )
                ],
            ),
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="send-1",
                        name="send_direct_message",
                        arguments={"recipient": "Alice", "body": body},
                    )
                ],
            ),
            ModelResponse(content="完成"),
        ]
    )
    core = AgentCore(model, channel=CollectChannel())
    core.register_tools([fetch_spec, send_spec])
    return asyncio.run(core.run(task)), sent, model


def test_slack_style_phishing_url_is_blocked_before_send() -> None:
    task = f"读取 {NEWS} 并把总结发给 Alice"
    result, sent, model = _run(task, f"总结见 {PHISH}")

    assert sent == [], "外发 handler 不应被调用"
    denied = [
        event
        for event in result.events
        if event.type == EventType.PERMISSION_DENIED
        and event.data.get("source") == "egress"
    ]
    assert denied, "事件流里必须有 source=egress 的 permission_denied"
    assert "不可信工具输出" in denied[0].data["reason"]

    last_messages = json.dumps(model.seen[-1], ensure_ascii=False)
    assert "外发被拒绝" in last_messages, "模型必须收到拒绝原因"
    assert PHISH in last_messages, "拒绝原因要指出被拦的 URL"


def test_url_present_in_task_is_allowed() -> None:
    task = f"读取 {NEWS}，确认 {PHISH} 后发给 Alice"
    result, sent, model = _run(task, f"总结见 {PHISH}")

    assert len(sent) == 1, "任务原文已包含该 URL 时不应误拦"
    assert not any(
        event.type == EventType.PERMISSION_DENIED
        and event.data.get("source") == "egress"
        for event in result.events
    )
    assert model.seen, "模型应正常完成"
