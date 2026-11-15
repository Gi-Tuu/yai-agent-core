"""AgentLoop 的规划/发现/工具 schema 聚焦逻辑。

从 ``kernel/loop.py`` 拆成 mixin；``AgentLoop`` 继承，测试仍可通过实例访问这些方法。
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from yai_core.diagnostics import DiagnosticKind, DiagnosticLevel
from yai_core.kernel.budget import BudgetExceeded
from yai_core.kernel.context import Context
from yai_core.kernel.tiering import STANDARD, STRONG, TierPolicy
from yai_core.types import AgentEvent, ChatMessage, EventType, ToolSpec


class PlanningMixin:
    # 由 AgentLoop 提供的状态/方法；这里只做类型声明。
    discovery: Any
    registry: Any
    model: Any
    full_schema_budget: int

    def _diag(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError

    async def _model_messages(self, ctx: Context) -> tuple[list[dict], AgentEvent | None]:
        raise NotImplementedError

    async def _model_chat(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def _compact_before_model(self, ctx: Context) -> AsyncIterator[AgentEvent]:
        raise NotImplementedError
        yield  # pragma: no cover

    def _with_meta_schemas(self, schemas: list[dict]) -> list[dict]:
        raise NotImplementedError

    def _discoverable_catalog(self) -> list[dict[str, str]] | None:
        """从发现源取"可按需启用"能力的轻量目录，过滤已注册名后交给路由。

        发现源未实现可选方法 ``list_discoverable``、或调用失败时返回 None，
        退化为"只在缺口出现后被动发现"的原有行为；该增强不构成依赖。
        只透传 name + 摘要（声明），handler 与授权都不在此暴露。
        """
        if self.discovery is None:
            return None
        getter = getattr(self.discovery, "list_discoverable", None)
        if not callable(getter):
            return None
        try:
            items = getter()
        except Exception as exc:  # noqa: BLE001 - 可发现目录是增强，失败静默退化
            self._diag(
                DiagnosticKind.DISCOVERABLE_LIST_FAILED,
                f"拉取可发现能力目录失败（{type(exc).__name__}），退化为被动发现",
                source=type(self.discovery).__name__,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        if not isinstance(items, list):
            # 返回 None / 非列表（表示无可发现项）同样退化为被动发现，不能崩溃。
            return None
        registered = {s.name for s in self.registry.all()}
        catalog = [
            {"name": str(it.get("name")), "summary": str(it.get("summary", ""))}
            for it in items
            if isinstance(it, dict) and it.get("name") and it.get("name") not in registered
        ]
        return catalog or None

    async def _discover_tools(self, need: str, task: str) -> AsyncIterator[AgentEvent]:
        """能力缺口出现时，向发现源要候选并注册：全程可观测、失败不致命。

        发现源只负责"返回候选规格"，注册去重由内核统一完成；
        工具真正执行时仍走 PermissionPolicy（发现不等于授权）。
        """
        if self.discovery is None:
            return
        try:
            candidates = await self.discovery.discover(
                need,
                task=task,
                available=[s.name for s in self.registry.all()],
            )
        except Exception as exc:  # noqa: BLE001 - 发现是增强不是依赖，失败不致命
            self._diag(
                DiagnosticKind.DISCOVERY_FAILED,
                f"工具发现源执行失败（{type(exc).__name__}），本次不注册新工具",
                level=DiagnosticLevel.ERROR,
                source=type(self.discovery).__name__,
                need=need,
                error=f"{type(exc).__name__}: {exc}",
            )
            yield AgentEvent(
                EventType.ERROR,
                {"stage": "tool_discovery", "error": f"{type(exc).__name__}: {exc}"},
            )
            return

        # 防御性去重：既跳过已注册，也跳过候选之间的同名工具。
        # 发现源返回畸形数据 / 注册失败都属于"增强失败"，必须就地降级，
        # 不能让异常穿透成"任务没有终态"（CORE-2）。
        new_specs: list[ToolSpec] = []
        try:
            seen: set[str] = set()
            for spec in candidates:
                if spec.name in seen or self.registry.has(spec.name):
                    continue
                seen.add(spec.name)
                new_specs.append(spec)
            if not new_specs:
                return
            self.registry.register_many(new_specs)
        except Exception as exc:  # noqa: BLE001 - 发现是增强不是依赖，失败不致命
            self._diag(
                DiagnosticKind.DISCOVERY_FAILED,
                f"工具发现结果处理失败（{type(exc).__name__}），本次不注册新工具",
                level=DiagnosticLevel.ERROR,
                source=type(self.discovery).__name__ if self.discovery else "",
                need=need,
                error=f"{type(exc).__name__}: {exc}",
            )
            yield AgentEvent(
                EventType.ERROR,
                {"stage": "tool_discovery", "error": f"{type(exc).__name__}: {exc}"},
            )
            return
        yield AgentEvent(
            EventType.TOOL_DISCOVERED,
            {
                "missing": need,
                "source": type(self.discovery).__name__ if self.discovery else "",
                "registered": [s.name for s in new_specs],
            },
        )

    async def _make_plan(
        self,
        ctx: Context,
        task: str,
        *,
        tier: str = STRONG,
        tier_policy: TierPolicy | None = None,
        tier_counts: dict[str, int] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """PLAN 策略的规划步：让模型把任务拆成 2-5 个编号步骤并写入上下文。

        正则提取步骤；一个都提取不到时退化为单步（任务本身），保证下游总有可
        执行计划。规划调用按成本感知策略给档（默认 strong、关闭时沿用路由档），
        并计入 ``tier_counts``，随后发 ``plan_created`` 事件。
        """
        # 规划也是一次模型调用：长历史先压成摘要，避免规划前就被硬裁剪删掉。
        async for compact_ev in self._compact_before_model(ctx):
            yield compact_ev
        prompt = (
            "把下面的任务拆成 2-5 个可执行步骤，每行一个步骤，用 1. 2. 3. 编号，"
            "只输出步骤本身：\n" + task
        )
        ctx.add(ChatMessage(role="user", content=prompt))
        # 档位：成本感知开启时由策略决定（规划默认 strong），否则沿用路由建议 tier。
        plan_tier = tier_policy.for_planning().tier if tier_policy is not None else tier
        messages, state_event = await self._model_messages(ctx)
        if state_event is not None:
            yield state_event
        resp = await self._model_chat(
            messages, None, tier=plan_tier, tier_counts=tier_counts
        )
        steps = [s.strip() for s in re.findall(r"\d+[.、)]\s*(.+)", resp.content)]
        if not steps:
            steps = [task]
        plan_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
        ctx.add(ChatMessage(role="assistant", content=plan_text))
        yield AgentEvent(EventType.PLAN_CREATED, {"steps": steps})

    async def _focus_tool_schemas(
        self, task: str, tier_counts: dict[str, int] | None = None
    ) -> tuple[list[dict], list[str] | None, dict[str, object] | None]:
        """两层工具目录的第二层：决定本轮注入哪些工具的完整 JSON Schema。

        返回 ``(schemas, focused_names, decision)``；``focused_names`` 为 None 表示全量
        （预算内或聚焦失败回退），否则是聚焦选中的工具名，供执行中发现新工具后
        把新 schema 追加进同一集合。``decision`` 只在实际聚焦调用后返回，由主循环
        发事件；selected 是本次实际注入的工具名快照（含保留的 meta-tool）。

        - 工具数在预算内：直接返回全部完整 schema（零额外模型调用，当前宿主都走这条）；
        - 工具数超预算：先让模型只看轻量目录、选出本任务要用的工具名，再用
          ``schemas_for`` 只注入这些工具的完整 schema，给大规模工具集上下文瘦身。
        - 选择失败 / 一个都没选中：回退全量 schema，宁多勿漏（瘦身为增强而非依赖）。
        """
        available_count = len(self.registry)
        if available_count <= self.full_schema_budget:
            return self.registry.llm_schemas(), None, None
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
            resp = await self._model_chat(
                [
                    {"role": "system", "content": "你只输出 JSON 工具名数组。"},
                    {"role": "user", "content": prompt},
                ],
                None,
                tier=STANDARD,
                tier_counts=tier_counts,
            )
            names = self._parse_tool_names(resp.content)
        except BudgetExceeded:
            # 预算熔断不是"聚焦失败"，必须继续向上冒泡到统一熔断点，不得回退全量。
            raise
        except Exception as exc:  # noqa: BLE001 - 聚焦是优化不是依赖，失败回退全量
            self._diag(
                DiagnosticKind.SCHEMA_FOCUS_FALLBACK,
                f"工具 schema 聚焦失败（{type(exc).__name__}），回退注入全量 schema",
                level=DiagnosticLevel.DEBUG,
                error=f"{type(exc).__name__}: {exc}",
            )
            names = []
            reason = "error"
        else:
            reason = "selected"
        chosen: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name in seen or not self.registry.has(name):
                continue
            seen.add(name)
            chosen.append(name)
        if chosen:
            # meta-tool 始终注入，聚焦裁剪只针对业务工具。
            schemas = self._with_meta_schemas(self.registry.schemas_for(chosen))
        else:
            schemas = self.registry.llm_schemas()
            if reason != "error":
                reason = "empty_selection"
        return schemas, chosen or None, {
            "mode": "focused" if chosen else "fallback",
            "reason": reason,
            "selected": [schema["function"]["name"] for schema in schemas],
            "available_count": available_count,
        }

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
