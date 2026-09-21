from __future__ import annotations

import inspect
import json
from typing import Any

from yai_core.spi import Channel, PermissionDecision, PermissionPolicy
from yai_core.tools.composer import resolve_args
from yai_core.tools.registry import ToolRegistry
from yai_core.types import AgentEvent, EventType, ToolSpec


class ToolExecutor:
    """Tool Bus：权限检查 -> 执行（同步/异步 handler 均可）-> 结构化结果。

    执行过程中产生的 Observer 事件由 execute 返回，
    由 AgentLoop 统一 yield（保证事件流只有一条路径）。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        channel: Channel,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.channel = channel

    async def execute(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[list[AgentEvent], bool, str]:
        """返回 (过程事件列表, 是否成功, 文本结果)，永不向 Agent Loop 抛异常。"""
        events: list[AgentEvent] = []
        if not self.registry.has(name):
            return events, False, f"工具 {name!r} 不存在"

        spec = self.registry.get(name)
        if spec.source == "composite" and spec.steps:
            return await self._execute_composite(spec, arguments)

        decision = await self.policy.check(name, arguments)
        if decision == PermissionDecision.ASK:
            events.append(
                AgentEvent(EventType.PERMISSION_ASKED, {"tool": name, "arguments": arguments})
            )
            approved = await self.channel.confirm(name, arguments)
            if not approved:
                return events, False, f"用户/宿主拒绝执行工具 {name}"
        if decision == PermissionDecision.DENY:
            return events, False, f"权限策略拒绝执行工具 {name}"

        events.append(AgentEvent(EventType.TOOL_CALL, {"tool": name, "arguments": arguments}))
        spec = self.registry.get(name)
        try:
            result = spec.handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            text = json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:  # noqa: BLE001 - 工具错误必须回灌给模型而不是崩溃
            events.append(
                AgentEvent(EventType.TOOL_RESULT, {"tool": name, "ok": False,
                                                   "error": str(exc)})
            )
            return events, False, f"工具 {name} 执行出错: {type(exc).__name__}: {exc}"

        events.append(
            AgentEvent(EventType.TOOL_RESULT, {"tool": name, "ok": True, "preview": text[:200]})
        )
        return events, True, text

    async def _execute_composite(
        self, spec: ToolSpec, arguments: dict[str, Any]
    ) -> tuple[list[AgentEvent], bool, str]:
        """执行组合工具：逐步编排内部工具，每一步都递归走完整权限链路。

        组合工具本身不产生新原始能力，因此不再对组合名做一次权限检查
        （那会让用户看到一个黑盒名字）；权限精确落到每一步的内部工具上。
        """
        events: list[AgentEvent] = []
        step_names = [step.tool for step in spec.steps]  # type: ignore[union-attr]
        events.append(
            AgentEvent(
                EventType.TOOL_COMPOSED,
                {"tool": spec.name, "steps": step_names, "arguments": arguments},
            )
        )

        step_results: list[Any] = []
        context = {"input": arguments, "steps": step_results}
        for index, step in enumerate(spec.steps):  # type: ignore[union-attr]
            try:
                resolved = resolve_args(step.args, context)
            except Exception as exc:  # noqa: BLE001 - 编排错误要回灌而非崩溃
                events.append(
                    AgentEvent(
                        EventType.TOOL_RESULT,
                        {"tool": spec.name, "ok": False,
                         "error": f"第 {index + 1} 步参数解析失败: {exc}"},
                    )
                )
                return events, False, f"组合工具 {spec.name} 第 {index + 1} 步参数解析失败: {exc}"

            sub_events, ok, sub_text = await self.execute(step.tool, resolved)
            events.extend(sub_events)
            if not ok:
                events.append(
                    AgentEvent(
                        EventType.TOOL_RESULT,
                        {"tool": spec.name, "ok": False,
                         "error": f"第 {index + 1} 步（{step.tool}）失败: {sub_text}"},
                    )
                )
                return (
                    events,
                    False,
                    f"组合工具 {spec.name} 在第 {index + 1} 步（{step.tool}）失败：{sub_text}",
                )
            try:
                step_results.append(json.loads(sub_text))
            except (json.JSONDecodeError, TypeError):
                step_results.append(sub_text)

        final = {"steps": step_results}
        text = json.dumps(final, ensure_ascii=False, default=str)
        events.append(
            AgentEvent(
                EventType.TOOL_RESULT, {"tool": spec.name, "ok": True, "preview": text[:200]}
            )
        )
        return events, True, text
