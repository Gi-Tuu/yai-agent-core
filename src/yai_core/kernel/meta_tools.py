"""AgentLoop 的 meta-tool 旁路：请求发现、创建代码工具、执行中重规划。

这些路径由模型显式调用 meta-tool 触发，不属于 direct/react/plan/clarify
主循环本身；抽到独立 mixin，主循环只保留调度与工具执行。方法仍通过
``self`` 使用 AgentLoop 的既有依赖，语义、事件顺序与话术保持原样。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from yai_core.kernel.context import Context
from yai_core.kernel.tiering import STRONG, TierPolicy
from yai_core.policy.authorize import authorize_tool_call, snapshot_arguments
from yai_core.tools.meta import CREATE_CODE_TOOL, REPLAN, REQUEST_CAPABILITY
from yai_core.types import (
    AgentEvent,
    ChatMessage,
    EventType,
    ModelResponse,
    Subject,
    ToolCallRequest,
)

if TYPE_CHECKING:
    from yai_core.spi import Channel, ToolDiscovery
    from yai_core.tools.executor import ToolExecutor
    from yai_core.tools.registry import ToolRegistry


class MetaToolRuntime(ABC):
    """AgentLoop 的 meta-tool 处理能力；依赖由 AgentLoop 初始化时注入。"""

    executor: ToolExecutor
    channel: Channel
    registry: ToolRegistry
    discovery: ToolDiscovery | None

    @abstractmethod
    async def _model_messages(
        self, ctx: Context
    ) -> tuple[list[dict], AgentEvent | None]:
        raise NotImplementedError

    @abstractmethod
    async def _model_chat(
        self, messages: list[dict], tools: list[dict] | None, *, tier: str
    ) -> ModelResponse:
        raise NotImplementedError

    @abstractmethod
    def _discover_tools(
        self, need: str, task: str
    ) -> AsyncIterator[AgentEvent]:
        # 调用直接返回异步迭代器，与子类含 yield 的 async def 签名一致。
        raise NotImplementedError

    async def _handle_meta_tool(
        self,
        call: ToolCallRequest,
        ctx: Context,
        task: str,
        replanned: list[int] | None = None,
        tier_policy: TierPolicy | None = None,
        tier_counts: dict[str, int] | None = None,
        subject: Subject | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """分发内核 meta-tool；未知 meta-tool 优雅降级为工具结果错误。"""
        if call.name == REQUEST_CAPABILITY:
            async for ev in self._handle_request_capability(call, ctx, task, subject):
                yield ev
            return
        if call.name == CREATE_CODE_TOOL:
            async for ev in self._handle_create_code_tool(call, ctx, subject):
                yield ev
            return
        if call.name == REPLAN:
            async for ev in self._handle_replan(
                call, ctx, task, replanned, tier_policy, tier_counts
            ):
                yield ev
            return
        # 理论上不可达（meta_tools 集合由内核控制），防御性回灌而非崩溃。
        ctx.add(
            ChatMessage(
                role="tool",
                content=f"内核不认识的 meta-tool：{call.name}",
                tool_call_id=call.id,
                name=call.name,
            )
        )

    async def _handle_request_capability(
        self,
        call: ToolCallRequest,
        ctx: Context,
        task: str,
        subject: Subject | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """执行中"请求发现新能力"：授权 -> 发缺口事件 -> 发现注册 -> 回灌结果。"""
        # meta-tool 同样先快照：审批看到的与内核实际执行的必须是同一份参数
        arguments = (
            snapshot_arguments(call.arguments)
            if isinstance(call.arguments, dict)
            else {}
        )
        auth_events, approved, reason = await authorize_tool_call(
            self.executor.policy,
            self.channel,
            REQUEST_CAPABILITY,
            arguments,
            subject=subject,
        )
        for ev in auth_events:
            yield ev

        def _reply(content: str) -> None:
            ctx.add(
                ChatMessage(
                    role="tool",
                    content=content,
                    tool_call_id=call.id,
                    name=call.name,
                )
            )

        if not approved:
            _reply(f"能力请求未被授权：{reason}。请基于现有工具给出结论或说明限制。")
            return
        need = str(arguments.get("need", "")).strip()
        if not need:
            _reply("缺少 need 参数：请用一句话描述你缺失但完成任务必需的能力。")
            return

        # 执行中能力缺口（区别于路由阶段，phase=react）。
        yield AgentEvent(
            EventType.CAPABILITY_MISSING,
            {
                "task": task,
                "missing": need,
                "available_tools": [s.name for s in self.registry.all()],
                "phase": "react",
            },
        )
        if self.discovery is None:
            _reply("宿主未配置能力发现源，无法发现新工具；请如实告知用户当前能力不足。")
            return

        before = {s.name for s in self.registry.all()}
        async for ev in self._discover_tools(need, task):
            yield ev
        registered = sorted({s.name for s in self.registry.all()} - before)
        if registered:
            _reply(
                "已发现并注册新工具："
                + ", ".join(registered)
                + "。请在下一轮直接调用其中合适的工具完成任务，不要重复请求发现。"
            )
        else:
            _reply(
                "未在宿主能力目录中找到匹配该需求的工具。请如实告知用户当前缺少该能力，"
                "不要编造工具或数据。"
            )

    async def _handle_create_code_tool(
        self,
        call: ToolCallRequest,
        ctx: Context,
        subject: Subject | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """执行中创建代码工具：授权 -> 校验 -> 注册（TTL 生命周期）-> 回灌结果。

        创建是敏感动作，除"无需审批"档外都要先授权；真正执行该工具时
        ToolExecutor 还会再走一次权限闸（创建与首次执行双重授权）。
        """
        # meta-tool 同样先快照：审批看到的与内核实际执行的必须是同一份参数
        arguments = (
            snapshot_arguments(call.arguments)
            if isinstance(call.arguments, dict)
            else {}
        )
        auth_events, approved, reason = await authorize_tool_call(
            self.executor.policy,
            self.channel,
            CREATE_CODE_TOOL,
            arguments,
            subject=subject,
        )
        for ev in auth_events:
            yield ev

        def _reply(content: str) -> None:
            ctx.add(
                ChatMessage(
                    role="tool",
                    content=content,
                    tool_call_id=call.id,
                    name=call.name,
                )
            )

        if not approved:
            _reply(f"创建代码工具未被授权：{reason}。请改用现有工具或组合工具。")
            return
        manager = self.executor.code_manager
        if manager is None:
            _reply("宿主未提供代码沙箱，无法创建代码工具；请改用组合工具 compose_tool。")
            return

        name = str(arguments.get("name", "")).strip()
        description = str(arguments.get("description", "")).strip()
        code = str(arguments.get("code", "")).strip()
        input_schema = arguments.get("input_schema")
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}
        try:
            spec = manager.create(name, description, input_schema, code)
        except ValueError as exc:
            _reply(f"代码工具创建失败：{exc}")
            return

        yield AgentEvent(
            EventType.CODE_TOOL_CREATED,
            {
                "name": spec.name,
                "source": "code",
                "ttl_seconds": manager.default_ttl,
                "permanent": False,
            },
        )
        _reply(
            f"已创建代码工具 {spec.name}（默认 {manager.default_ttl // 3600} 小时有效，"
            "每次被调用都会刷新存活期；后台可将其设为永久保留）。请在下一轮直接调用它，"
            "首次执行仍会按权限策略确认，且代码在宿主隔离沙箱中运行。"
        )

    async def _handle_replan(
        self,
        call: ToolCallRequest,
        ctx: Context,
        task: str,
        replanned: list[int] | None,
        tier_policy: TierPolicy | None,
        tier_counts: dict[str, int] | None,
    ) -> AsyncIterator[AgentEvent]:
        """执行中重规划：标注调整 -> 产出修正计划 -> 回灌继续（不走授权闸）。

        replan 只重排内部计划、不产生外部副作用，因此不要求用户再授权；
        内核保留已完成步骤、基于当前观察产出修正计划，并发 ``plan_revised`` 事件。
        """
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        reason = str(arguments.get("reason", "")).strip() or "执行中发现原计划需要调整"
        revision = 1
        if replanned is not None:
            replanned[0] += 1
            revision = replanned[0]
        # 重规划属于复杂拆解：成本感知开启时由策略给档（默认 strong），否则 strong。
        plan_tier = (
            tier_policy.for_planning().tier if tier_policy is not None else STRONG
        )
        if tier_counts is not None:
            tier_counts[plan_tier] = tier_counts.get(plan_tier, 0) + 1
        messages, state_event = await self._model_messages(ctx)
        if state_event is not None:
            yield state_event
        # 当前批次尚未回齐结果；规划副本只保留已完成配对，不改主上下文的调用批次。
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("tool_calls"):
                completed_ids = {
                    message["tool_call_id"]
                    for message in messages[index + 1 :]
                    if message["role"] == "tool"
                }
                assistant = dict(messages[index])
                completed_calls = [
                    item
                    for item in assistant["tool_calls"]
                    if item["id"] in completed_ids
                ]
                if completed_calls:
                    assistant["tool_calls"] = completed_calls
                    messages[index] = assistant
                elif assistant.get("content"):
                    del assistant["tool_calls"]
                    messages[index] = assistant
                else:
                    del messages[index]
                break
        messages.append(
            {
                "role": "user",
                "content": (
                    "【系统重规划】原计划执行到此因“" + reason + "”需要调整。"
                    "请保留已经拿到结果、无需重做的步骤，基于当前已获得的观察，"
                    "重新输出 2-5 步编号计划（每行一个步骤，用 1. 2. 编号），"
                    "不要重复已完成步骤；只输出步骤本身。"
                ),
            }
        )
        resp = await self._model_chat(messages, None, tier=plan_tier)
        steps = [s.strip() for s in re.findall(r"\d+[.、)]\s*(.+)", resp.content)]
        if not steps:
            steps = [task]
        plan_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
        yield AgentEvent(
            EventType.PLAN_REVISED,
            {"revision": revision, "reason": reason, "steps": steps},
        )
        # 回灌 tool 结果：模型下一轮按新计划继续执行。
        ctx.add(
            ChatMessage(
                role="tool",
                content=(
                    "计划已按当前观察修订（第 " + str(revision) + " 版）。\n"
                    + plan_text
                    + "\n"
                    "请按新计划继续调用工具执行，不要重复已完成步骤，"
                    "拿到全部结果后给出最终汇报。"
                ),
                tool_call_id=call.id,
                name=call.name,
            )
        )
