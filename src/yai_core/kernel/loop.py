"""Agent Loop：按 Adaptive Router 选定的策略执行任务。

- direct: 一次模型调用直接回答
- react:  推理 <-> 工具调用循环，直到模型不再调用工具
- plan:   先生成步骤计划，再带计划进入 react 循环
- clarify: 通过 Channel 反问后重新路由
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterable

from yai_core.kernel.context import Context
from yai_core.kernel.router import AdaptiveRouter
from yai_core.policy.authorize import authorize_tool_call
from yai_core.spi import (
    Channel,
    MemoryStore,
    ModelProvider,
    ToolDiscovery,
)
from yai_core.tools.executor import ToolExecutor
from yai_core.tools.meta import CREATE_CODE_TOOL, REQUEST_CAPABILITY
from yai_core.tools.registry import ToolRegistry
from yai_core.types import (
    AgentEvent,
    ChatMessage,
    EventType,
    Strategy,
    ToolCallRequest,
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
        full_schema_budget: int = 24,
        meta_tools: Iterable[str] = (),
        # max_clarify_rounds：澄清反问的最大轮数（正整数）；0 或负数等价于不澄清、
        # 路由判 clarify 时立即降级直接回答。
        # full_schema_budget：工具数不超过该值时，一次性把全部完整 JSON Schema
        # 注入 function-calling（零额外模型调用，当前宿主规模都走这条）；超过后
        # 改为"先看轻量目录选工具、再注入所选完整 schema"的两层懒加载，避免上百
        # 工具把上下文撑爆。聚焦失败一律回退全量，宁多勿漏。
        # meta_tools：由内核直接拦截处理的工具名（如 request_capability），
        # 它们注册进目录让模型可见，但不进 ToolExecutor 的普通 handler 执行路径。
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
        self.full_schema_budget = full_schema_budget
        self.meta_tools: set[str] = set(meta_tools)

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

        # 两层工具目录：系统提示只放 name + 一句话摘要（轻量目录），
        # 完整参数结构在 function-calling 的 tools 参数里按需注入，见 _react_cycle。
        ctx = Context(_SYSTEM_TEMPLATE.format(tools=self.registry.catalog_text()))
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
        async for ev in self._react_cycle(
            ctx, use_tools=strategy != Strategy.DIRECT, task=task
        ):
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

    async def _focus_tool_schemas(
        self, task: str
    ) -> tuple[list[dict], list[str] | None]:
        """两层工具目录的第二层：决定本轮注入哪些工具的完整 JSON Schema。

        返回 ``(schemas, focused_names)``；``focused_names`` 为 None 表示全量
        （预算内或聚焦失败回退），否则是聚焦选中的工具名，供执行中发现新工具后
        把新 schema 追加进同一集合。

        - 工具数在预算内：直接返回全部完整 schema（零额外模型调用，当前宿主都走这条）；
        - 工具数超预算：先让模型只看轻量目录、选出本任务要用的工具名，再用
          ``schemas_for`` 只注入这些工具的完整 schema，给大规模工具集上下文瘦身。
        - 选择失败 / 一个都没选中：回退全量 schema，宁多勿漏（瘦身为增强而非依赖）。
        """
        if len(self.registry) <= self.full_schema_budget:
            return self.registry.llm_schemas(), None
        catalog = self.registry.catalog_text()
        prompt = (
            "下面是宿主可用工具的能力目录（名称: 一句话摘要）：\n"
            f"{catalog}\n\n"
            "为完成任务，请只输出你确定会调用的工具名称，输出 JSON 字符串数组，"
            '例如 ["search_notes", "save_memory"]，不要输出任何其他内容；'
            "拿不准的工具也请一并选上，避免漏掉必要能力。\n"
            f"任务：{task}"
        )
        try:
            resp = await self.model.achat(
                [
                    {"role": "system", "content": "你只输出 JSON 工具名数组。"},
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                tier="standard",
            )
            names = self._parse_tool_names(resp.content)
        except Exception:  # noqa: BLE001 - 聚焦是优化不是依赖，失败回退全量
            return self.registry.llm_schemas(), None
        chosen: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name in seen or not self.registry.has(name):
                continue
            seen.add(name)
            chosen.append(name)
        if not chosen:
            return self.registry.llm_schemas(), None
        return self.registry.schemas_for(chosen), chosen

    @staticmethod
    def _parse_tool_names(content: str) -> list[str]:
        """从模型输出里容错提取工具名 JSON 数组；解析失败返回空列表（触发回退）。"""
        match = re.search(r"\[.*\]", content, flags=re.DOTALL)
        if match is None:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        return [str(x).strip() for x in data if isinstance(x, str) and str(x).strip()]

    async def _react_cycle(
        self, ctx: Context, *, use_tools: bool, task: str = ""
    ) -> AsyncIterator[AgentEvent]:
        if use_tools:
            tools_schema, focused_names = await self._focus_tool_schemas(task)
        else:
            tools_schema, focused_names = None, None
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
            failed: list[str] = []
            newly_registered: list[str] = []
            for call in resp.tool_calls:
                if call.name in self.meta_tools:
                    # 内核 meta-tool（请求发现/创建工具）由 loop 直接处理，
                    # 可能在执行中动态注册新工具，不进普通执行器。
                    async for ev in self._handle_meta_tool(call, ctx, task):
                        yield ev
                        if ev.type == EventType.TOOL_DISCOVERED:
                            newly_registered.extend(ev.data.get("registered", []))
                        elif ev.type == EventType.CODE_TOOL_CREATED:
                            newly_registered.append(ev.data.get("name", ""))
                    continue
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
                if not ok:
                    failed.append(f"{call.name}：{result_text}")

            # 执行中发现的新工具：下一轮 function-calling 必须能看到其完整 schema，
            # 否则模型"发现了却调不到"。全量模式重算；聚焦模式把新名字并入聚焦集。
            if newly_registered and use_tools:
                if focused_names is None:
                    tools_schema = self.registry.llm_schemas()
                else:
                    for name in newly_registered:
                        if name not in focused_names:
                            focused_names.append(name)
                    tools_schema = self.registry.schemas_for(focused_names)

            if failed:
                # 轻量反思（不额外调用模型）：把失败观察回灌成一条明确引导，
                # 让模型下一轮自我修正——换工具、请求发现新能力，或如实报告能力缺口，
                # 而不是重复失败调用。
                ctx.add(
                    ChatMessage(
                        role="user",
                        content=(
                            "【系统反思提示】上一步工具调用未成功："
                            + "；".join(failed)
                            + "。请判断：若能用其他现有工具完成，就改用其他工具，"
                            "不要重复同一失败调用；若宿主确实缺少所需能力，"
                            "可调用 request_capability 请求发现新工具，"
                            "否则请直接说明缺失了什么能力，并给出当前能给出的结论。"
                        ),
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

    async def _handle_meta_tool(
        self,
        call: ToolCallRequest,
        ctx: Context,
        task: str,
    ) -> AsyncIterator[AgentEvent]:
        """分发内核 meta-tool；未知 meta-tool 优雅降级为工具结果错误。"""
        if call.name == REQUEST_CAPABILITY:
            async for ev in self._handle_request_capability(call, ctx, task):
                yield ev
            return
        if call.name == CREATE_CODE_TOOL:
            async for ev in self._handle_create_code_tool(call, ctx):
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
    ) -> AsyncIterator[AgentEvent]:
        """执行中"请求发现新能力"：授权 -> 发缺口事件 -> 发现注册 -> 回灌结果。"""
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        auth_events, approved, reason = await authorize_tool_call(
            self.executor.policy, self.channel, REQUEST_CAPABILITY, arguments
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
    ) -> AsyncIterator[AgentEvent]:
        """执行中创建代码工具：授权 -> 校验 -> 注册（TTL 生命周期）-> 回灌结果。

        创建是敏感动作，除"无需审批"档外都要先授权；真正执行该工具时
        ToolExecutor 还会再走一次权限闸（创建与首次执行双重授权）。
        """
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        auth_events, approved, reason = await authorize_tool_call(
            self.executor.policy, self.channel, CREATE_CODE_TOOL, arguments
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
