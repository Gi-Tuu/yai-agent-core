from __future__ import annotations

import asyncio
import functools
import inspect
import json
from collections.abc import Callable
from typing import Any

from yai_core.policy.authorize import authorize_tool_call, snapshot_arguments
from yai_core.policy.redact import redact_arguments
from yai_core.spi import Channel, PermissionPolicy, ToolSandbox
from yai_core.tools.code_tools import CodeToolManager
from yai_core.tools.composer import resolve_args
from yai_core.tools.registry import ToolRegistry
from yai_core.types import AgentEvent, EventType, ToolSpec

#: 代码工具在沙箱里的最长执行秒数（默认值，可由宿主覆盖）。
DEFAULT_CODE_TIMEOUT = 10.0

#: 单个工具结果回灌进对话上下文的默认字符上界，与 OpenAPI 集成的 max_result_chars 对齐。
#: 作用在"工具结果 → 模型上下文"这一层，比 Context 总量事后整轮裁剪更精准；
#: None 表示关闭（宿主自行负责结果上界）。
DEFAULT_TOOL_RESULT_CHARS = 16_000


def bound_tool_result(
    text: str, limit: int | None
) -> tuple[str, bool, int]:
    """把单个工具结果限制在 ``limit`` 字符内；返回 ``(text, truncated, original_chars)``。

    - ``limit=None`` 或未超限时原样返回，``truncated=False``；
    - 超限时保留"头 + 尾"，中间插入显式省略标记，让模型明确知道结果被截断
      （与 payload_guard 的有界裁剪理念一致，绝不静默删改）；
    - 保证返回文本长度严格不超过 ``limit``。
    """
    original_chars = len(text)
    if limit is None or original_chars <= limit:
        return text, False, original_chars

    head_budget = int(limit * 0.7)
    tail_budget = limit - head_budget
    head = text[:head_budget]
    tail = text[original_chars - tail_budget :]

    def _join(head_part: str) -> str:
        omitted = original_chars - len(head_part) - tail_budget
        marker = f"\n…[工具结果过长，已省略 {omitted} 字符，以下为结尾部分]…\n"
        return head_part + marker + tail

    bounded = _join(head)
    # marker 长度随 omitted 位数变化，必要时从头部再收紧，确保严格不越上界。
    while len(bounded) > limit and head_budget > 0:
        head_budget -= 1
        head = text[:head_budget]
        bounded = _join(head)
    return bounded, True, original_chars


class ToolTimeoutError(RuntimeError):
    """工具 handler 超过 ``handler_timeout``。"""


def _consume_abandoned_result(future: asyncio.Future[Any]) -> None:
    """被超时放弃的线程任务：把结果/异常取出来丢掉。

    不取的话，后台线程之后抛错会留下 "exception was never retrieved" 的噪音，
    运维看到会以为还有一个没处理的故障。
    """
    if not future.cancelled():
        future.exception()


def _check_timeout(value: Any, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{label} 必须是正数或 None")


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
        *,
        sandbox: ToolSandbox | None = None,
        code_manager: CodeToolManager | None = None,
        code_timeout: float = DEFAULT_CODE_TIMEOUT,
        result_char_limit: int | None = DEFAULT_TOOL_RESULT_CHARS,
        handler_timeout: float | None = None,
    ) -> None:
        if result_char_limit is not None and (
            isinstance(result_char_limit, bool) or result_char_limit <= 0
        ):
            raise ValueError("result_char_limit 必须是正整数或 None")
        _check_timeout(handler_timeout, "handler_timeout")
        self.registry = registry
        self.policy = policy
        self.channel = channel
        self.sandbox = sandbox
        self.code_manager = code_manager
        self.code_timeout = code_timeout
        # 单个工具结果回灌上下文前的统一上界（None=关闭）。
        self.result_char_limit = result_char_limit
        # 普通工具 handler 的墙上超时（None=关闭，默认关闭以保证零行为变化）。
        # 代码工具的超时走 code_timeout + 沙箱，两者是不同链路。
        self.handler_timeout = handler_timeout

    async def _invoke_handler(self, handler: Callable[..., Any], arguments: dict) -> Any:
        """调用 handler；只有配了 ``handler_timeout`` 才包上超时，默认路径与从前一字不差。

        两种形状的区别必须说清楚，不能一起承诺：

        - ``async def`` handler：``wait_for`` 会真的取消它，协程收到 ``CancelledError``；
        - 同步 handler：只能丢进线程池，**线程杀不掉**。这里刻意不用 ``wait_for``——
          它会 await 被取消的 future，等于一直等线程跑完（实测 0.4s 的活儿要满 0.4s
          才回话）；改用 ``asyncio.wait`` 拿到"到点未完成"就立刻放弃，回话不延迟。

        同步路径还剩一处诚实的边界：被放弃的线程仍在**循环默认线程池**里，
        ``asyncio.run`` / 事件循环收尾会 join 它（asyncio 的行为，不绕过）。
        所以"同步工具超时"是**给模型的止损**，不是进程级立即脱身；
        真正可能长时间阻塞的能力应当实现成 ``async def``。
        """
        timeout = self.handler_timeout
        if inspect.iscoroutinefunction(handler):
            if timeout is None:
                return await handler(**arguments)
            try:
                return await asyncio.wait_for(handler(**arguments), timeout)
            except TimeoutError as exc:
                raise ToolTimeoutError(
                    f"超过 {timeout:g}s 未完成，已取消（异步工具会被真正中断）"
                ) from exc
        if timeout is None:
            result = handler(**arguments)
            return await result if inspect.isawaitable(result) else result
        # 同步 handler 只能丢进线程池，而线程杀不掉。这里刻意**不用 wait_for**：
        # wait_for 会 await 被取消的 future，等于一直等线程跑完（实测 0.4s 的活儿
        # 要满 0.4s 才回话）。改用 asyncio.wait 拿到"到点但没完成"的结果后直接放弃，
        # 立刻给模型回话；线程仍在后台跑完，所以话术必须说清"可能仍有副作用"。
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, functools.partial(handler, **arguments))
        future.add_done_callback(_consume_abandoned_result)
        done, _pending = await asyncio.wait({future}, timeout=timeout)
        if not done:
            raise ToolTimeoutError(
                f"超过 {timeout:g}s 未完成；同步工具无法中断，"
                "它可能仍在后台跑完并产生副作用，重试前先确认状态"
            )
        outcome = future.result()
        # 少见形状：同步函数返回 awaitable。此时已花掉的时间不计，但语义要保持正确。
        return await outcome if inspect.isawaitable(outcome) else outcome

    def _bound(self, text: str) -> tuple[str, bool, int]:
        return bound_tool_result(text, self.result_char_limit)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        subject: Any = None,
    ) -> tuple[list[AgentEvent], bool, str]:
        """返回 (过程事件列表, 是否成功, 文本结果)，永不向 Agent Loop 抛异常。

        ``subject`` 是宿主注入的可信操作主体（``None`` = 本上下文没有主体）；
        它只交给声明了 ``yai_subject_aware`` 的策略，以及带能力声明的工具做准入。
        """
        events: list[AgentEvent] = []
        if not self.registry.has(name):
            return events, False, f"工具 {name!r} 不存在"

        spec = self.registry.get(name)
        # 审批-执行-审计共用一份快照：宿主/通道在 confirm 期间改写参数不会生效
        arguments = snapshot_arguments(arguments)

        # 准入判定在权限闸之前：工具自带的 gate 说这次调用不该到达业务操作时，
        # 连"要不要问人"都不必问；内核不认识判定来自哪个协议。
        gate_fn = spec.governance.gate
        decision = gate_fn(arguments, subject) if gate_fn is not None else None
        if decision is not None and decision.reject:
            return events, False, decision.reject

        if spec.source == "composite" and spec.steps:
            return await self._execute_composite(spec, arguments, subject)

        auth_events, approved, reason = await authorize_tool_call(
            self.policy,
            self.channel,
            name,
            arguments,
            sensitive_paths=spec.governance.sensitive,
            audit_sensitive=spec.governance.audit_sensitive,
            subject=subject,
            require_approval=decision.require_approval if decision else False,
            approval_reason=decision.reason if decision else "",
            approval_prompt=decision.prompt if decision else "",
            risk=decision.risk if decision else "",
            scope=decision.scope if decision else "",
        )
        events.extend(auth_events)
        if not approved:
            return events, False, reason

        if spec.source == "code":
            return await self._execute_code(spec, arguments, events)

        handler = spec.handler
        if handler is None:
            return events, False, f"工具 {name} 没有可执行 handler"
        try:
            result = await self._invoke_handler(handler, arguments)
            text = json.dumps(result, ensure_ascii=False, default=str)
        except ToolTimeoutError as exc:
            # 超时单独成支：给模型的话术要说明"这次没做成、且可能仍在后台跑完"，
            # 不能和"执行出错"混成一句，否则模型只会原样重试。
            events.append(
                AgentEvent(
                    EventType.TOOL_RESULT,
                    {"tool": name, "ok": False, "error": str(exc), "timed_out": True},
                )
            )
            return events, False, f"工具 {name} 执行失败: {exc}"
        except Exception as exc:  # noqa: BLE001 - 工具错误必须回灌给模型而不是崩溃
            events.append(
                AgentEvent(EventType.TOOL_RESULT, {"tool": name, "ok": False,
                                                   "error": str(exc)})
            )
            return events, False, f"工具 {name} 执行出错: {type(exc).__name__}: {exc}"

        text, truncated, result_chars = self._bound(text)
        events.append(
            AgentEvent(
                EventType.TOOL_RESULT,
                {
                    "tool": name,
                    "ok": True,
                    "preview": text[:200],
                    "truncated": truncated,
                    "result_chars": result_chars,
                },
            )
        )
        return events, True, text

    async def _execute_code(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        events: list[AgentEvent],
    ) -> tuple[list[AgentEvent], bool, str]:
        """执行代码工具：生命周期校验后交给宿主沙箱，内核不内置执行器。"""
        # 1) TTL 生命周期：被调用即刷新；已过期则回收并提示重新创建。
        if self.code_manager is not None and not self.code_manager.touch(spec.name):
            self.registry.unregister(spec.name)
            msg = f"代码工具 {spec.name} 已超过存活期被回收，请重新创建"
            events.append(
                AgentEvent(
                    EventType.TOOL_RESULT,
                    {"tool": spec.name, "ok": False, "error": msg},
                )
            )
            return events, False, msg

        # 2) 宿主未提供沙箱：优雅降级（能力缺失），而不是崩溃。
        if self.sandbox is None:
            msg = "宿主未提供代码沙箱（ToolSandbox），代码工具不可用"
            events.append(
                AgentEvent(
                    EventType.TOOL_RESULT,
                    {"tool": spec.name, "ok": False, "error": msg},
                )
            )
            return events, False, msg

        # 3) 交给宿主沙箱执行（无网/无敏感文件/超时由沙箱保证）。
        try:
            sb = await self.sandbox.execute(
                spec.code or "", inputs=arguments, timeout=self.code_timeout
            )
        except Exception as exc:  # noqa: BLE001 - 沙箱异常必须回灌而不是穿透
            events.append(
                AgentEvent(
                    EventType.TOOL_RESULT,
                    {"tool": spec.name, "ok": False, "error": f"沙箱调用失败: {exc}"},
                )
            )
            return events, False, f"工具 {spec.name} 沙箱调用失败: {exc}"

        if not sb.ok:
            events.append(
                AgentEvent(
                    EventType.TOOL_RESULT,
                    {"tool": spec.name, "ok": False, "error": sb.error or "沙箱执行失败"},
                )
            )
            return events, False, f"工具 {spec.name} 沙箱执行失败: {sb.error}"

        text = json.dumps(sb.output, ensure_ascii=False, default=str)
        text, truncated, result_chars = self._bound(text)
        events.append(
            AgentEvent(
                EventType.TOOL_RESULT,
                {
                    "tool": spec.name,
                    "ok": True,
                    "preview": text[:200],
                    "truncated": truncated,
                    "result_chars": result_chars,
                },
            )
        )
        return events, True, text

    async def _execute_composite(
        self, spec: ToolSpec, arguments: dict[str, Any], subject: Any = None
    ) -> tuple[list[AgentEvent], bool, str]:
        """执行组合工具：逐步编排内部工具，每一步都递归走完整权限链路。

        组合工具本身不产生新原始能力，因此不再对组合名做一次权限检查
        （那会让用户看到一个黑盒名字）；权限精确落到每一步的内部工具上。
        """
        events: list[AgentEvent] = []
        steps = spec.steps
        if not steps:
            return events, False, f"组合工具 {spec.name} 缺少步骤"
        step_names = [step.tool for step in steps]
        # 对外副本按**组合工具自己的声明**脱敏（与 authorize 同一口径）：
        # 宿主在清单上写了 sensitive，就不能因为这条路走了组合分支而失效。
        # 执行用的 arguments 保持真值，脱敏只影响"别人看见什么"。
        shown = redact_arguments(
            arguments,
            spec.governance.sensitive,
            broad=spec.governance.audit_sensitive,
        )
        events.append(
            AgentEvent(
                EventType.TOOL_COMPOSED,
                {"tool": spec.name, "steps": step_names, "arguments": shown},
            )
        )

        step_results: list[Any] = []
        context = {"input": arguments, "steps": step_results}
        for index, step in enumerate(steps):
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

            sub_events, ok, sub_text = await self.execute(
                step.tool, resolved, subject=subject
            )
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
        text, truncated, result_chars = self._bound(text)
        events.append(
            AgentEvent(
                EventType.TOOL_RESULT,
                {
                    "tool": spec.name,
                    "ok": True,
                    "preview": text[:200],
                    "truncated": truncated,
                    "result_chars": result_chars,
                },
            )
        )
        return events, True, text
