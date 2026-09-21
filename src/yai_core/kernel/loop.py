"""Agent Loop：按 Adaptive Router 选定的策略执行任务。

- direct: 一次模型调用直接回答
- react:  推理 <-> 工具调用循环，直到模型不再调用工具
- plan:   先生成步骤计划，再带计划进入 react 循环
- clarify: 通过 Channel 反问后重新路由
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator

from yai_core.kernel.context import Context
from yai_core.kernel.router import AdaptiveRouter
from yai_core.spi import Channel, MemoryStore, ModelProvider, ToolDiscovery
from yai_core.tools.executor import ToolExecutor
from yai_core.tools.registry import ToolRegistry
from yai_core.types import (
    AgentEvent,
    ChatMessage,
    EventType,
    Strategy,
    ToolSpec,
)

_SYSTEM_TEMPLATE = """你是运行在宿主软件内部的 AI 助手。\
你可以使用宿主提供的工具（见下方工具目录）。\
如果现有工具不足以完成任务，请明确指出缺失的能力，系统会尝试为你发现或补充新工具。\
不要编造工具不存在的数据。当任务完成时，直接给出可交付的最终结果。

宿主当前提供的能力：
{tools}
"""

# 澄清反问的固定话术：不调用模型，离线可测；明确告诉用户需要补什么。
# 第 2 条把领域名词降级为举例——内核是通用的，不能写死某个宿主的业务对象。
_CLARIFY_QUESTION = (
    "我没完全理解“{task}”该怎么执行。请补充：\n"
    "1）你想做什么（查询 / 统计 / 记录 / 新建 / 完成）；\n"
    "2）涉及哪个对象或哪件事（例如某位客户、某类订单、某个待办）。"
)
# 澄清卡片里内嵌的任务最大长度，超长截断，避免长任务把宿主 UI 撑爆。
_CLARIFY_TASK_PREVIEW_CHARS = 80
# 澄清被放弃（空回答/超时/用户终止）时的收尾文案。
_CLARIFY_ABORT_TEXT = (
    "任务信息不足，已停止。请换个说法重新描述你想做什么，"
    "例如“统计华东区硬件订单的销售额”。"
)


class AgentLoop:
    def __init__(
        self,
        model: ModelProvider,
        registry: ToolRegistry,
        executor: ToolExecutor,
        channel: Channel,
        memory: MemoryStore,
        router: AdaptiveRouter | None = None,
        discovery: ToolDiscovery | None = None,
        *,
        max_iters: int = 6,
        max_clarify_rounds: int = 2,
        # max_clarify_rounds：澄清反问的最大轮数（正整数）；0 或负数等价于不澄清、
        # 路由判 clarify 时立即降级直接回答。当前未从 AgentCore 透传，与 max_iters 现状一致。
    ) -> None:
        self.model = model
        self.registry = registry
        self.executor = executor
        self.channel = channel
        self.memory = memory
        self.router = router or AdaptiveRouter()
        # 工具发现源：默认 None（安全默认，只发缺口事件、不动态注册）；
        # 宿主显式注入（如 StaticCatalog）后，缺口出现时才会发现并注册新工具。
        self.discovery = discovery
        self.max_iters = max_iters
        self.max_clarify_rounds = max_clarify_rounds

    async def astream(self, task: str, *, _clarify_depth: int = 0) -> AsyncIterator[AgentEvent]:
        decision = await self.router.aclassify(task, self.registry)
        strategy = decision.strategy
        # 决策来源（llm/rules）、理由与模型档位随事件流出：每次自适应决策都可审计。
        yield AgentEvent(
            EventType.STRATEGY_SELECTED,
            {
                "strategy": strategy.value,
                "source": decision.source,
                "reason": decision.reason,
                "tier": decision.tier,
            },
        )

        # 能力缺口感知：LLM 路由明确判定现有工具不足时，向宿主发事件，
        # 宿主可据此引导安装插件/接入 MCP；规则路径、澄清、无工具部署形态不发。
        if (
            decision.missing_capability
            and strategy != Strategy.CLARIFY
            and len(self.registry) > 0
        ):
            yield AgentEvent(
                EventType.CAPABILITY_MISSING,
                {
                    "task": task,
                    "missing": decision.missing_capability,
                    "available_tools": [s.name for s in self.registry.all()],
                    "strategy": strategy.value,
                },
            )
            # 配置了发现源时，把"缺口"闭环成"新工具"：发现 -> 注册 -> 本轮即可用。
            # 注册发生在系统提示词构建之前，react/plan 本轮就能看到新工具。
            if self.discovery is not None:
                async for ev in self._discover_tools(decision.missing_capability, task):
                    yield ev

        if strategy == Strategy.CLARIFY and _clarify_depth >= self.max_clarify_rounds:
            # 澄清轮数达上限仍不明确：降级为直接回答（DIRECT，不调工具），由模型说明
            # 信息不足——信息不全时不应硬调工具，这是有意为之；绝不无限反问。
            # 每次策略变化都发事件，tier 沿用路由决策，保证决策可审计。
            strategy = Strategy.DIRECT
            yield AgentEvent(
                EventType.STRATEGY_SELECTED,
                {
                    "strategy": Strategy.DIRECT.value,
                    "source": "rules",
                    "reason": f"已澄清 {_clarify_depth} 轮仍不明确，降级直接回答",
                    "tier": decision.tier,
                },
            )

        if strategy == Strategy.CLARIFY:
            preview = task.strip()
            if len(preview) > _CLARIFY_TASK_PREVIEW_CHARS:
                preview = preview[:_CLARIFY_TASK_PREVIEW_CHARS] + "…"
            question = _CLARIFY_QUESTION.format(task=preview)
            yield AgentEvent(
                EventType.CLARIFY_REQUESTED,
                {"question": question, "round": _clarify_depth + 1},
            )
            answer = (await self.channel.ask(question) or "").strip()
            if not answer:
                # 空回答 / 超时 / 用户终止：体面收尾，不再递归。
                yield AgentEvent(EventType.MODEL_MESSAGE, {"text": _CLARIFY_ABORT_TEXT})
                await self.memory.append_history(ChatMessage(role="user", content=task))
                await self.memory.append_history(
                    ChatMessage(role="assistant", content=_CLARIFY_ABORT_TEXT)
                )
                yield AgentEvent(
                    EventType.DONE,
                    {"strategy": Strategy.CLARIFY.value, "final_text": _CLARIFY_ABORT_TEXT},
                )
                return
            # 关键：把原任务与历次补充累积起来再重新路由，避免"问意图→问公司"的丢上下文乒乓。
            merged = f"{task}\n补充信息：{answer}"
            async for ev in self.astream(merged, _clarify_depth=_clarify_depth + 1):
                yield ev
            return

        ctx = Context(_SYSTEM_TEMPLATE.format(tools=self.registry.describe()))
        for msg in self.memory.history():
            ctx.add(msg)
        ctx.add(ChatMessage(role="user", content=task))

        if strategy == Strategy.PLAN:
            async for ev in self._make_plan(ctx, task, tier=decision.tier):
                yield ev
            # 计划只是"助手说过的话"，必须再推一把，模型才会进入工具执行；
            # 否则真实模型会把计划本身当成最终答复（离线脚本模型曾掩盖此问题）。
            ctx.add(
                ChatMessage(
                    role="user",
                    content="请按上面的计划逐步调用工具执行，拿到全部结果后给出最终汇报。",
                )
            )

        final_text = ""
        async for ev in self._react_cycle(ctx, use_tools=strategy != Strategy.DIRECT):
            yield ev
            if ev.type == EventType.MODEL_MESSAGE:
                final_text = ev.data.get("text", final_text)

        await self.memory.append_history(ChatMessage(role="user", content=task))
        await self.memory.append_history(ChatMessage(role="assistant", content=final_text))
        yield AgentEvent(EventType.DONE, {"strategy": strategy.value, "final_text": final_text})

    async def _discover_tools(
        self, need: str, task: str
    ) -> AsyncIterator[AgentEvent]:
        """能力缺口出现时，向发现源要候选并注册：全程可观测、失败不致命。

        发现源只负责"返回候选规格"，注册去重由内核统一完成；
        工具真正执行时仍走 PermissionPolicy（发现不等于授权）。
        """
        try:
            candidates = await self.discovery.discover(
                need,
                task=task,
                available=[s.name for s in self.registry.all()],
            )
        except Exception as exc:  # noqa: BLE001 - 发现是增强不是依赖，失败不致命
            yield AgentEvent(
                EventType.ERROR,
                {"stage": "tool_discovery", "error": f"{type(exc).__name__}: {exc}"},
            )
            return

        # 防御性去重：既跳过已注册，也跳过候选之间的同名工具。
        new_specs: list[ToolSpec] = []
        seen: set[str] = set()
        for spec in candidates:
            if spec.name in seen or self.registry.has(spec.name):
                continue
            seen.add(spec.name)
            new_specs.append(spec)
        if not new_specs:
            return

        self.registry.register_many(new_specs)
        yield AgentEvent(
            EventType.TOOL_DISCOVERED,
            {
                "missing": need,
                "source": type(self.discovery).__name__,
                "registered": [s.name for s in new_specs],
            },
        )

    async def _make_plan(
        self, ctx: Context, task: str, *, tier: str = "strong"
    ) -> AsyncIterator[AgentEvent]:
        prompt = (
            "把下面的任务拆成 2-5 个可执行步骤，每行一个步骤，用 1. 2. 3. 编号，"
            "只输出步骤本身：\n" + task
        )
        ctx.add(ChatMessage(role="user", content=prompt))
        # 档位由路由器建议（LLM 路由可给 standard/strong），规则路由的 plan 默认 strong。
        resp = await self.model.achat(ctx.llm_messages(), tools=None, tier=tier)
        steps = [s.strip() for s in re.findall(r"\d+[.、)]\s*(.+)", resp.content)]
        if not steps:
            steps = [task]
        plan_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
        ctx.add(ChatMessage(role="assistant", content=plan_text))
        yield AgentEvent(EventType.PLAN_CREATED, {"steps": steps})

    async def _react_cycle(
        self, ctx: Context, *, use_tools: bool
    ) -> AsyncIterator[AgentEvent]:
        tools_schema = self.registry.llm_schemas() if use_tools else None
        for _ in range(self.max_iters):
            try:
                resp = await self.model.achat(ctx.llm_messages(), tools=tools_schema)
            except Exception as exc:  # noqa: BLE001
                yield AgentEvent(EventType.ERROR, {"error": f"{type(exc).__name__}: {exc}"})
                return

            if not resp.tool_calls:
                text = resp.content.strip()
                ctx.add(ChatMessage(role="assistant", content=text))
                yield AgentEvent(EventType.MODEL_MESSAGE, {"text": text})
                return

            ctx.add(
                ChatMessage(
                    role="assistant",
                    content=resp.content,
                    tool_calls=[
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(
                                tc.arguments, ensure_ascii=False
                            )},
                        }
                        for tc in resp.tool_calls
                    ],
                )
            )
            for call in resp.tool_calls:
                tool_events, ok, result_text = await self.executor.execute(
                    call.name, call.arguments
                )
                for tool_event in tool_events:
                    yield tool_event
                ctx.add(
                    ChatMessage(
                        role="tool",
                        content=result_text,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )

        # 达到迭代上限：要求模型基于已有观察收尾
        ctx.add(
            ChatMessage(
                role="user",
                content="已达到工具调用上限，请基于已有结果直接给出最终答案。",
            )
        )
        resp = await self.model.achat(ctx.llm_messages(), tools=None)
        yield AgentEvent(EventType.MODEL_MESSAGE, {"text": resp.content.strip()})
