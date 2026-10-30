from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from yai_core.kernel.budget import BudgetExceeded
from yai_core.policy.approval import (
    DECISION_APPROVAL_REQUIRED,
    DECISION_REJECT_INVALID_ARGUMENTS,
    argument_errors,
    evaluate_approval,
)
from yai_core.policy.authorize import (
    approval_context,
    authorize_tool_call,
    confirm_with_context,
    fill_prompt,
    policy_decision,
    snapshot_arguments,
)
from yai_core.policy.egress import check_egress, record_tool_output
from yai_core.policy.redact import redact_arguments
from yai_core.spi import Channel, PermissionDecision, PermissionPolicy, ToolSandbox
from yai_core.tools.code_tools import CodeToolManager
from yai_core.tools.composer import resolve_args
from yai_core.tools.execution_audit import (
    ExecutionAudit,
    event_error,
    event_preview,
    infer_status,
    replay_preview,
)
from yai_core.tools.registry import ToolRegistry
from yai_core.types import (
    AgentEvent,
    ChangePreview,
    EventType,
    GateDecision,
    ReceiptStatus,
    ToolSpec,
)

#: 代码工具在沙箱里的最长执行秒数（默认值，可由宿主覆盖）。
DEFAULT_CODE_TIMEOUT = 10.0

#: 单个工具结果回灌进对话上下文的默认字符上界，与 OpenAPI 集成的 max_result_chars 对齐。
#: 作用在"工具结果 → 模型上下文"这一层，比 Context 总量事后整轮裁剪更精准；
#: None 表示关闭（宿主自行负责结果上界）。
DEFAULT_TOOL_RESULT_CHARS = 16_000

#: 进程内幂等结果缓存上限。只缓存宿主显式提供 idempotency_key 的调用；
#: 跨进程/多实例去重仍由宿主负责。
IDEMPOTENCY_CACHE_LIMIT = 256


def _fingerprint_arguments(arguments: dict) -> str:
    """参数指纹：同一个幂等键只认同一组参数，避免撞键时返回别人的结果。"""
    raw = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _composite_timeout_message(name: str, declared_ms: int) -> str:
    """组合超时的统一话术：只停止等待，不承诺终止同步步骤的副作用。"""
    return (
        f"组合工具 {name} 超过声明的 {declared_ms}ms 未完成，已停止等待（不是强制终止）；"
        "已完成步骤的副作用不会回滚，异步步骤会被取消，"
        "同步步骤无法中断、可能仍在后台跑完"
    )


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


class _PreviewError(RuntimeError):
    """宿主声明的变更预览无法生成；审批不能建立在未知变更上。"""


class _IdempotencyError(RuntimeError):
    """宿主声明的幂等键生成器失败；不能假装这次调用可去重。"""


@dataclass(frozen=True)
class _PermissionDependency:
    """仅存于进程内缓存的权限依据，不进入事件、收据或模型正文。"""

    name: str
    spec: ToolSpec
    arguments: dict[str, Any] | None


@dataclass(frozen=True)
class _CacheLookup:
    """一次幂等查找的结果；hit 非空表示可直接返回 (ok, text)。"""

    key: str
    fingerprint: str
    hit: tuple[bool, str] | None = None


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


def _native_approval(spec: ToolSpec, arguments: dict[str, Any]) -> GateDecision | None:
    """宿主只写了数据（``governance.approval``）时，由内核自己判一次要不要问人。

    没声明审批就返回 ``None``（"这层没意见"），事件形状与加了这块功能之前一字不差；
    ``allow`` 同样折回 ``None``，不让"声明了但没命中"污染审计载荷。
    """
    policy = spec.governance.approval
    if policy is None:
        return None
    outcome = evaluate_approval(policy, arguments, spec.input_schema)
    if outcome.decision == DECISION_REJECT_INVALID_ARGUMENTS:
        return GateDecision(reject=f"调用被审批闸拒绝：{outcome.reason}", reason=outcome.reason)
    if outcome.decision == DECISION_APPROVAL_REQUIRED:
        return GateDecision(
            require_approval=True, reason=outcome.reason, prompt=outcome.prompt
        )
    return None


def _tighten(
    gate: GateDecision | None, native: GateDecision | None
) -> GateDecision | None:
    """合并同一工具的函数级闸（``gate``）与数据级闸（``approval``），**取更严的那个**。

    两个来源都可能同时存在：``gate`` 由宿主/集成层现场算，``approval`` 是随清单走的声明。
    谁都不比谁弱，所以不能"有 gate 就当 approval 不存在"——那等于声明了的保护内核没做。
    合并只会让闸门更严：拒绝取并、要问人取或、话术与描述性字段先到先得。
    """
    if gate is None:
        return native
    if native is None:
        return gate
    rejects = [text for text in (gate.reject, native.reject) if text]
    reasons = [text for text in (gate.reason, native.reason) if text]
    return GateDecision(
        reject="；".join(dict.fromkeys(rejects)),
        require_approval=gate.require_approval or native.require_approval,
        reason="；".join(dict.fromkeys(reasons)),
        prompt=gate.prompt or native.prompt,
        risk=gate.risk or native.risk,
        scope=gate.scope or native.scope,
        target=gate.target or native.target,
    )


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
        self._audit = ExecutionAudit(registry)
        # 普通工具 handler 的墙上超时（None=关闭，默认关闭以保证零行为变化）。
        # 代码工具的超时走 code_timeout + 沙箱，两者是不同链路。
        self.handler_timeout = handler_timeout
        # 每工具的频率上限闸（进程内滑动窗口）：key=工具名 → 时间戳队列。
        # 只在工具自己声明了 rate_limit 时才用得上，未声明的工具零开销。
        self._rate_hits: dict[str, deque[float]] = {}
        self._rate_lock = threading.Lock()
        # 每条结果同时绑定参数指纹和首次调用的权限依赖（含嵌套子调用）。
        # 依赖随同一 LRU 条目淘汰，不另建无界缓存。
        self._idempotent_cache: OrderedDict[
            tuple[str, str],
            tuple[bool, str, ReceiptStatus, str, str, tuple[_PermissionDependency, ...]],
        ] = OrderedDict()

    def _effective_timeout(self, spec: ToolSpec) -> float | None:
        """这次调用的实际上墙超时：全局 ``handler_timeout`` 与工具自声明的更严一档。

        工具自声明（外部能力契约 ``execution.timeout_ms``）只会**收紧**不会放宽——
        放宽属于"放松"，而治理数据只能加严。
        """
        declared = spec.governance.timeout_ms
        if declared is None:
            return self.handler_timeout
        per_tool = declared / 1000.0
        if self.handler_timeout is None:
            return per_tool
        return min(self.handler_timeout, per_tool)

    def _rate_limit_exceeded(self, name: str, hint: Any) -> bool:
        """按工具自声明的频率上限判定本次调用是否超限（超限 = 不执行）。

        进程内滑动窗口（多实例部署要跨实例总闸，那需要状态共享，不在这一层）。
        窗口串认不出来时**不拦**（提示无法执行不等于授权执行——但反过来也不该
        悄悄当成"没限"；这里选择不拦并让宿主从声明里看到原文）。
        """
        seconds = hint.window_seconds()
        if seconds is None:
            return False
        now = time.monotonic()
        cutoff = now - seconds
        with self._rate_lock:
            hits = self._rate_hits.setdefault(name, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= hint.count:
                return True
            hits.append(now)
            return False

    async def _invoke_handler(
        self, handler: Callable[..., Any], arguments: dict, timeout: float | None = None
    ) -> Any:
        """调用 handler；只有配了 ``handler_timeout`` 才包上超时，默认路径与从前一字不差。

        两种形状的区别必须说清楚，不能一起承诺：

        - ``async def`` handler：``asyncio.timeout`` 会真的取消它，协程收到 ``CancelledError``；
        - 同步 handler：只能丢进线程池，**线程杀不掉**。这里刻意不用 ``wait_for``——
          它会 await 被取消的 future，等于一直等线程跑完（实测 0.4s 的活儿要满 0.4s
          才回话）；改用 ``asyncio.wait`` 拿到"到点未完成"就立刻放弃，回话不延迟。

        同步路径还剩一处诚实的边界：被放弃的线程仍在**循环默认线程池**里，
        ``asyncio.run`` / 事件循环收尾会 join 它（asyncio 的行为，不绕过）。
        所以"同步工具超时"是**给模型的止损**，不是进程级立即脱身；
        真正可能长时间阻塞的能力应当实现成 ``async def``。
        """
        timeout = self.handler_timeout if timeout is None else timeout
        if inspect.iscoroutinefunction(handler):
            if timeout is None:
                return await handler(**arguments)
            try:
                # asyncio.timeout 而非 asyncio.wait_for：3.11 的 wait_for 会把协程
                # 包进额外 Task，且在内层恰好完成时吞掉外层取消（见 kernel/loop.py
                # 同处注释）。工具超时必须只终止这一次调用，不能连带把"取消整个
                # 子任务"也吃掉。
                async with asyncio.timeout(timeout):
                    return await handler(**arguments)
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
        deadline = loop.time() + timeout
        future = loop.run_in_executor(None, functools.partial(handler, **arguments))
        future.add_done_callback(_consume_abandoned_result)
        done, _pending = await asyncio.wait(
            {future}, timeout=max(0.0, deadline - loop.time())
        )
        if not done:
            raise ToolTimeoutError(
                f"超过 {timeout:g}s 未完成；同步工具无法中断，"
                "它可能仍在后台跑完并产生副作用，重试前先确认状态"
            )
        outcome = future.result()
        if not inspect.isawaitable(outcome):
            return outcome
        # 少见形状：同步函数返回 awaitable。它同样是这次调用的执行时间，
        # 不能因为函数本身已经返回就绕开超时；剩余额度用尽也要如实止损。
        remaining = deadline - loop.time()
        if remaining <= 0:
            if inspect.iscoroutine(outcome):
                outcome.close()
            raise ToolTimeoutError(
                f"超过 {timeout:g}s 未完成；同步工具返回的异步结果未被等待，"
                "它可能仍在后台跑完并产生副作用，重试前先确认状态"
            )
        try:
            async with asyncio.timeout(remaining):
                return await outcome
        except TimeoutError as exc:
            raise ToolTimeoutError(
                f"超过 {timeout:g}s 未完成；同步工具返回的异步结果已取消，"
                "底层操作可能仍有副作用，重试前先确认状态"
            ) from exc

    def _bound(self, text: str) -> tuple[str, bool, int]:
        return bound_tool_result(text, self.result_char_limit)

    async def _generate_preview(
        self, spec: ToolSpec, arguments: dict, subject: Any
    ) -> ChangePreview | None:
        """调用宿主声明的预览生成器；支持同步/异步，任何异常都失败关闭。"""
        provider = spec.governance.preview
        if provider is None:
            return None
        try:
            value = provider(arguments, subject)
            if inspect.isawaitable(value):
                value = await value
            return ChangePreview.from_value(value)
        except Exception as exc:  # noqa: BLE001 - 预览失败不能让审批无依据地继续
            raise _PreviewError(
                f"变更预览生成失败：{type(exc).__name__}: {exc}"
            ) from exc

    async def _idempotency_key(
        self, spec: ToolSpec, arguments: dict, subject: Any
    ) -> str:
        """宿主声明的幂等键；未声明或返回空串表示不去重。"""
        provider = spec.governance.idempotency_key
        if provider is None:
            return ""
        try:
            value = provider(arguments, subject)
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:  # noqa: BLE001 - 键生成失败不能假装可去重
            raise _IdempotencyError(
                f"幂等键生成失败：{type(exc).__name__}: {exc}"
            ) from exc
        if value is None:
            return ""
        if not isinstance(value, str) or not value.strip():
            raise _IdempotencyError("幂等键必须是非空字符串（或 None）")
        return value.strip()

    def _cache_get(
        self, tool: str, key: str, fingerprint: str
    ) -> tuple[
        tuple[bool, str, ReceiptStatus, str, tuple[_PermissionDependency, ...]], bool
    ] | None:
        """返回 (缓存结果及权限依赖, 参数是否与首次执行一致)。"""
        if not key:
            return None
        cached = self._idempotent_cache.pop((tool, key), None)
        if cached is None:
            return None
        self._idempotent_cache[(tool, key)] = cached
        ok, text, status, error, cached_fingerprint, dependencies = cached
        return (ok, text, status, error, dependencies), cached_fingerprint == fingerprint

    def _cache_put(
        self,
        tool: str,
        key: str,
        fingerprint: str,
        *,
        ok: bool,
        text: str,
        status: ReceiptStatus,
        error: str = "",
        dependencies: list[_PermissionDependency],
    ) -> None:
        if not key or status == "not_started":
            return
        self._idempotent_cache.pop((tool, key), None)
        self._idempotent_cache[(tool, key)] = (
            ok, text, status, error, fingerprint, tuple(dependencies)
        )
        while len(self._idempotent_cache) > IDEMPOTENCY_CACHE_LIMIT:
            self._idempotent_cache.popitem(last=False)

    async def _replay_dependency_rejection(
        self, dependencies: tuple[_PermissionDependency, ...], subject: Any
    ) -> str:
        """只复核当前准入，不审批、不生成预览、不解析步骤、更不执行工具。"""
        for dependency in dependencies:
            name, spec = dependency.name, dependency.spec
            if (
                not self.registry.has(name)
                or self.registry.get(name) is not spec
                or spec.name != name
            ):
                return f"缓存依赖工具 {name} 已注销或替换，拒绝重放"
            if dependency.arguments is None:
                return f"缓存依赖工具 {name} 的参数快照不可用，拒绝重放"
            # 首个依赖（被重放的工具自己）也必须复核：外层准入用的是**原始形态**
            # 参数，而 gate 回调可能原地规范化后再执行（如去空格）——撤权若按
            # 规范化后的名字生效，只查原始形态就会漏掉，已撤权结果被照常交付。
            # 依赖记录存的就是过闸后的有效形态，这里用同一形态复核。
            try:
                # 每个回调得到独立副本，不能污染缓存或后续校验的权限依据。
                gate = spec.governance.gate
                decision = (
                    gate(deepcopy(dependency.arguments), subject) if gate else None
                )
                decision = _tighten(
                    decision, _native_approval(spec, deepcopy(dependency.arguments))
                )
                if decision is not None and decision.reject:
                    return f"缓存依赖工具 {name} 的准入闸拒绝重放"
                if argument_errors(deepcopy(dependency.arguments), spec.input_schema):
                    return f"缓存依赖工具 {name} 的调用参数不合法，拒绝重放"
                if await policy_decision(
                    self.policy, name, deepcopy(dependency.arguments), subject
                ) == PermissionDecision.DENY:
                    return f"权限策略拒绝重放缓存依赖工具 {name}"
            except Exception:  # noqa: BLE001 - 复核失败关闭，不泄漏参数/异常正文
                return f"缓存依赖工具 {name} 的权限复核失败，拒绝重放"
        return ""

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        subject: Any = None,
        budget: Any = None,
        call_id: str = "",
        _trace: list[_PermissionDependency] | None = None,
        _timeout: float | None = None,
    ) -> tuple[list[AgentEvent], bool, str]:
        """对外入口：执行一次工具调用，必要时做"执行后验证"。

        ``unknown``（超时 / 同步 handler 被放弃 / handler 抛错 / 沙箱异常）意味着
        **副作用可能已经发生**。如果工具声明了 ``postcondition``，这里会跑一次宿主
        提供的检查来确认"到底发生了没有"：确认发生 → 收据升级为 ``succeeded`` 并标
        ``verified=True``；确认没发生或检查失败 → 保持 ``unknown``，只记录
        ``postcondition`` 的结果，绝不替宿主猜。
        """
        # 单一权威快照：后续的 gate 原地规范化、通道回调改写、执行、后置条件
        # 验证全部作用于同一份副本——调用方的字典从此不被改写，验证的也是
        # "实际执行的那组参数"而不是调用方稍后可能改掉的对象。
        arguments = snapshot_arguments(arguments)
        events, ok, text = await self._execute_impl(
            name,
            arguments,
            subject=subject,
            budget=budget,
            call_id=call_id,
            _trace=_trace,
            _timeout=_timeout,
        )
        if not ok:
            ok, text = await self._audit.apply_postcondition(name, arguments, events, ok, text)
            # 失败文案（错误消息）里也可能带 URL；成功路径已在 _execute_impl
            # 按结构化结果记录，不再用 JSON 转义后的文本重复记录。
            record_tool_output(text)
        return events, ok, text

    def _check_admission(
        self,
        spec: ToolSpec,
        name: str,
        arguments: dict[str, Any],
        subject: Any,
        events: list[AgentEvent],
        started: float,
        call_id: str,
    ) -> tuple[GateDecision | None, str | None]:
        """工具自带闸、参数校验、频率上限三道前置闸。"""
        gate_fn = spec.governance.gate
        decision = gate_fn(arguments, subject) if gate_fn is not None else None
        decision = _tighten(decision, _native_approval(spec, arguments))
        if decision is not None and decision.reject:
            events.append(
                self._audit.denied_event(spec, name, arguments, decision.reject, "gate")
            )
            self._audit.fail(
                events, spec=spec, name=name, call_id=call_id,
                started=started, message=decision.reject,
            )
            return decision, decision.reject

        egress_reason = check_egress(spec, arguments)
        if egress_reason is not None:
            events.append(
                self._audit.denied_event(spec, name, arguments, egress_reason, "egress")
            )
            self._audit.fail(
                events, spec=spec, name=name, call_id=call_id,
                started=started, message=egress_reason,
            )
            return decision, egress_reason

        errors = argument_errors(arguments, spec.input_schema)
        if errors:
            message = f"工具 {name} 的调用参数不合法：" + "；".join(errors)
            self._audit.fail(
                events,
                spec=spec,
                name=name,
                call_id=call_id,
                started=started,
                message=message,
                flags={"invalid_arguments": True},
            )
            return decision, message

        rate_hint = spec.governance.rate_limit
        if rate_hint is not None and self._rate_limit_exceeded(name, rate_hint):
            message = (
                f"工具 {name} 超出调用频率上限"
                f"（{rate_hint.count} 次 / {rate_hint.window}），本次未执行"
            )
            events.append(
                AgentEvent(
                    EventType.TOOL_RESULT,
                    {"tool": name, "ok": False, "error": message, "rate_limited": True},
                )
            )
            events.append(
                self._audit.denied_event(spec, name, arguments, message, "rate_limit")
            )
            self._audit.attach(
                events,
                self._audit.receipt(
                    call_id=call_id,
                    tool=name,
                    status="not_started",
                    started=started,
                    error=message,
                ),
            )
            return decision, message
        return decision, None

    async def _lookup_cache(
        self,
        spec: ToolSpec,
        name: str,
        arguments: dict[str, Any],
        subject: Any,
        events: list[AgentEvent],
        trace: list[_PermissionDependency],
        trace_start: int,
        started: float,
        call_id: str,
    ) -> _CacheLookup:
        """幂等键生成与缓存重放；命中时直接给出 (ok, text)。"""
        try:
            idempotency_key = await self._idempotency_key(spec, arguments, subject)
        except _IdempotencyError as exc:
            message = str(exc)
            self._audit.fail(
                events,
                spec=spec,
                name=name,
                call_id=call_id,
                started=started,
                message=message,
                flags={"receipt_failed": True},
            )
            return _CacheLookup("", "", (False, message))

        fingerprint = _fingerprint_arguments(arguments) if idempotency_key else ""
        cached = self._cache_get(name, idempotency_key, fingerprint)
        if cached is None:
            return _CacheLookup(idempotency_key, fingerprint)

        cached_result, same_arguments = cached
        ok, cached_text, cached_status, cached_error, dependencies = cached_result
        # 子缓存也必须把历史依赖传给父组合，包括父组合稍后超时的路径。
        trace[trace_start:] = dependencies
        if not same_arguments:
            message = (
                f"幂等键 {idempotency_key!r} 已用于另一组参数，拒绝重放；"
                "不同操作请使用不同的幂等键"
            )
            self._audit.fail(
                events,
                spec=spec,
                name=name,
                call_id=call_id,
                started=started,
                key=idempotency_key,
                message=message,
                flags={"idempotency_conflict": True},
            )
            return _CacheLookup(idempotency_key, fingerprint, (False, message))

        # 重放只交付已有结果，不再弹审批；但当前策略的 DENY 仍然生效——
        # 缓存是去重，不是绕过权限收紧的后门。
        if (
            await policy_decision(self.policy, name, arguments, subject)
            == PermissionDecision.DENY
        ):
            message = f"权限策略拒绝执行工具 {name}"
            events.append(
                self._audit.denied_event(spec, name, arguments, message, "policy")
            )
            self._audit.fail(
                events,
                spec=spec,
                name=name,
                call_id=call_id,
                started=started,
                key=idempotency_key,
                message=message,
                flags={"replay_denied": True},
            )
            return _CacheLookup(idempotency_key, fingerprint, (False, message))
        rejection = await self._replay_dependency_rejection(dependencies, subject)
        if rejection:
            events.append(self._audit.denied_event(
                spec, name, arguments, rejection, "replay_dependency"
            ))
            self._audit.fail(
                events,
                spec=spec,
                name=name,
                call_id=call_id,
                started=started,
                key=idempotency_key,
                message=rejection,
                flags={"replay_denied": True},
            )
            return _CacheLookup(idempotency_key, fingerprint, (False, rejection))

        receipt = self._audit.receipt(
            call_id=call_id,
            tool=name,
            status=cached_status,
            started=started,
            key=idempotency_key,
            error=cached_error,
            replayed=True,
        )
        payload: dict[str, Any] = {
            "tool": name,
            "ok": ok,
            "replayed": True,
            "receipt": receipt.to_dict(),
        }
        if ok:
            payload["preview"] = replay_preview(spec, cached_text)
            payload["truncated"] = False
            payload["result_chars"] = len(cached_text)
        else:
            payload["error"] = event_error(spec, cached_text)
        events.append(AgentEvent(EventType.TOOL_RESULT, payload))
        return _CacheLookup(idempotency_key, fingerprint, (ok, cached_text))

    async def _preview_or_reject(
        self,
        spec: ToolSpec,
        name: str,
        arguments: dict[str, Any],
        subject: Any,
        events: list[AgentEvent],
        started: float,
        call_id: str,
        key: str,
    ) -> tuple[ChangePreview | None, str | None]:
        """生成审批预览；失败时按失败关闭返回拒绝原因。"""
        try:
            return await self._generate_preview(spec, arguments, subject), None
        except _PreviewError as exc:
            message = str(exc)
            self._audit.fail(
                events,
                spec=spec,
                name=name,
                call_id=call_id,
                started=started,
                key=key,
                message=message,
                flags={"preview_failed": True},
            )
            return None, message

    async def _recheck_approved_preview(
        self,
        spec: ToolSpec,
        name: str,
        arguments: dict[str, Any],
        subject: Any,
        preview: ChangePreview | None,
        auth_events: list[AgentEvent],
        events: list[AgentEvent],
        started: float,
        call_id: str,
        key: str,
    ) -> str | None:
        """审批后复核预览 token：目标或参数变了就不能执行。"""
        if preview is None or not any(
            event.type == EventType.PERMISSION_ASKED for event in auth_events
        ):
            return None
        message, flags = await self._approved_preview_failure(
            spec, arguments, subject, preview
        )
        if message is not None:
            self._audit.fail(
                events, spec=spec, name=name, call_id=call_id,
                started=started, key=key, message=message, flags=flags,
            )
        return message

    async def _approved_preview_failure(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        subject: Any,
        preview: ChangePreview,
    ) -> tuple[str | None, dict[str, Any]]:
        """只复核并返回原因和标记；事件与收据归调用方所有。"""
        try:
            current_preview = await self._generate_preview(spec, arguments, subject)
        except _PreviewError as exc:
            return str(exc), {"preview_failed": True}
        assert current_preview is not None
        if current_preview.token != preview.token:
            return (
                "审批所依据的变更预览已失效（确认后目标或参数发生了变化），"
                "本次未执行；请重新发起以查看最新预览并再次确认",
                {"preview_stale": True},
            )
        return None, {}

    def _finalize_execution(
        self,
        events: list[AgentEvent],
        *,
        name: str,
        call_id: str,
        started: float,
        key: str,
        fingerprint: str,
        dependencies: list[_PermissionDependency],
        ok: bool,
        text: str,
        status: ReceiptStatus,
    ) -> tuple[list[AgentEvent], bool, str]:
        """统一密封/挂载执行结果，缓存保留原始错误与独立依赖快照。"""
        error = "" if ok else text
        self._audit.attach(events, self._audit.receipt(
            call_id=call_id, tool=name, status=status, started=started,
            key=key, error=error,
        ))
        if status != "not_started":
            self._cache_put(
                name, key, fingerprint, dependencies=dependencies,
                ok=ok, text=text, status=status, error=error,
            )
        return events, ok, text

    async def _execute_impl(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        subject: Any = None,
        budget: Any = None,
        call_id: str = "",
        _trace: list[_PermissionDependency] | None = None,
        _timeout: float | None = None,
    ) -> tuple[list[AgentEvent], bool, str]:
        """返回 (过程事件列表, 是否成功, 文本结果)。

        普通执行错误一律回灌给模型、不向上抛；唯一的例外是任务级
        :class:`~yai_core.kernel.budget.BudgetExceeded`——它是硬熔断，必须冒泡到
        Agent Loop 统一收尾，不能被当成"某次工具失败"吞掉。

        ``subject`` 是宿主注入的可信操作主体（``None`` = 本上下文没有主体）；
        它只交给声明了 ``yai_subject_aware`` 的策略，以及带能力声明的工具做准入。

        ``budget`` 是可选的 :class:`~yai_core.kernel.budget.BudgetTracker`。预算
        计数下沉到这里，是为了让组合工具的**每个真实步骤**都过同一道闸——只数
        顶层调用会让"一次组合 = 三次业务操作"绕过 max_tool_calls。

        ``call_id`` 来自模型的工具调用 id，只用于执行收据/关联审计；直接调用
        执行器的宿主可以留空。``_timeout`` 是组合步骤的剩余预算上限（内核内部
        使用），与工具自身声明/全局 ``handler_timeout`` 取更严的一档。
        """
        started = time.monotonic()
        events: list[AgentEvent] = []
        if not self.registry.has(name):
            return self._audit.fail(
                events, spec=None, name=name, call_id=call_id,
                started=started, message=f"工具 {name!r} 不存在",
            )

        spec = self.registry.get(name)
        # arguments 已是 execute() 的单一权威快照：审批-执行-审计共用同一份，
        # 宿主/通道在 confirm 期间、gate 在准入期间的原地改写都只作用于这份副本。
        # 递归调用共用 trace，进入子调用时立即记录，取消/超时也不会丢失依赖。
        # 参数来自 resolve_args 的真值，不从脱敏事件或截断结果反推。
        trace = [] if _trace is None else _trace
        trace_start = len(trace)
        try:
            dependency_arguments = deepcopy(arguments)
        except Exception:  # noqa: BLE001 - 不可复制参数的结果不能安全重放
            dependency_arguments = None
        trace.append(_PermissionDependency(name, spec, dependency_arguments))

        decision, denial = self._check_admission(
            spec, name, arguments, subject, events, started, call_id
        )
        if denial is not None:
            return events, False, denial

        # 组合工具自身不占一次业务调用（真实操作在每一步），但预算已耗尽时
        # 不该先弹审批卡再在第一步失败——这里只做前置检查，不计数。
        if budget is not None:
            budget.before_tool_call()

        lookup = await self._lookup_cache(
            spec, name, arguments, subject, events, trace, trace_start, started, call_id
        )
        idempotency_key, fingerprint = lookup.key, lookup.fingerprint
        if lookup.hit is not None:
            return events, *lookup.hit

        preview, denial = await self._preview_or_reject(
            spec, name, arguments, subject, events, started, call_id, idempotency_key
        )
        if denial is not None:
            return events, False, denial

        if spec.source == "composite" and spec.steps:
            declared = spec.governance.timeout_ms
            if declared is None:
                comp_events, ok, text = await self._execute_composite(
                    spec,
                    arguments,
                    subject,
                    decision,
                    budget=budget,
                    events=events,
                    preview=preview,
                    call_id=call_id,
                    trace=trace,
                )
            else:
                timeout = declared / 1000.0
                try:
                    async with asyncio.timeout(timeout):
                        comp_events, ok, text = await self._execute_composite(
                            spec,
                            arguments,
                            subject,
                            decision,
                            budget=budget,
                            events=events,
                            preview=preview,
                            call_id=call_id,
                            trace=trace,
                        )
                except TimeoutError:
                    message = _composite_timeout_message(spec.name, declared)
                    events.append(
                        AgentEvent(
                            EventType.TOOL_RESULT,
                            {
                                "tool": spec.name,
                                "ok": False,
                                "error": message,
                                "timed_out": True,
                            },
                        )
                    )
                    return self._finalize_execution(
                        events, name=name, call_id=call_id, started=started,
                        key=idempotency_key, fingerprint=fingerprint,
                        dependencies=trace[trace_start:],
                        ok=False, text=message, status="unknown",
                    )
            status = infer_status(comp_events, ok=ok, text=text, source="composite")
            return self._finalize_execution(
                comp_events, name=name, call_id=call_id, started=started,
                key=idempotency_key, fingerprint=fingerprint,
                dependencies=trace[trace_start:], ok=ok, text=text, status=status,
            )

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
            target=decision.target if decision else "",
            preview=preview,
        )
        events.extend(auth_events)
        if not approved:
            source = (
                "approval"
                if any(e.type == EventType.PERMISSION_ASKED for e in auth_events)
                else "policy"
            )
            events.append(self._audit.denied_event(spec, name, arguments, reason, source))
            return self._audit.fail(
                events, spec=spec, name=name, call_id=call_id,
                started=started, key=idempotency_key, message=reason,
            )
        # 依赖记录刷新为"过闸后的有效形态"：gate / 通道回调可能原地规范化参数
        # （如去空格），实际执行的就是这份；撤权复核与重放检查必须用同一形态，
        # 否则已撤权的结果会因复核看到原始形态而被照常交付。
        try:
            trace[trace_start] = _PermissionDependency(
                name, spec, deepcopy(arguments)
            )
        except Exception:  # noqa: BLE001 - 刷新失败退回准入前快照，不阻塞执行
            pass

        denial = await self._recheck_approved_preview(
            spec,
            name,
            arguments,
            subject,
            preview,
            auth_events,
            events,
            started,
            call_id,
            idempotency_key,
        )
        if denial is not None:
            return events, False, denial

        if spec.source == "code":
            if budget is not None:
                try:
                    budget.record_tool_call()
                except BudgetExceeded as exc:
                    # 硬熔断仍要冒泡，但已密封的事件（含本步之前的子收据）
                    # 挂到异常上随行，由 AgentLoop 补发，收据链不缺环。
                    exc.partial_events = list(events)
                    raise
            code_events, ok, text = await self._execute_code(spec, arguments, events)
            status = infer_status(code_events, ok=ok, text=text, source="code")
            return self._finalize_execution(
                code_events, name=name, call_id=call_id, started=started,
                key=idempotency_key, fingerprint=fingerprint,
                dependencies=trace[trace_start:], ok=ok, text=text, status=status,
            )

        handler = spec.handler
        if handler is None:
            message = f"工具 {name} 没有可执行 handler"
            events.append(
                AgentEvent(
                    EventType.TOOL_RESULT,
                    {"tool": name, "ok": False, "error": message},
                )
            )
            self._audit.attach(
                events,
                self._audit.receipt(
                    call_id=call_id,
                    tool=name,
                    status="not_started",
                    started=started,
                    key=idempotency_key,
                    error=message,
                ),
            )
            return events, False, message
        if budget is not None:
            try:
                budget.record_tool_call()
            except BudgetExceeded as exc:
                # 同上：熔断冒泡前把已密封事件挂到异常上，避免收据链缺环。
                exc.partial_events = list(events)
                raise
        call_timeout = self._effective_timeout(spec)
        if _timeout is not None:
            call_timeout = (
                _timeout if call_timeout is None else min(call_timeout, _timeout)
            )
        try:
            result = await self._invoke_handler(handler, arguments, timeout=call_timeout)
            # 不可信 URL 来源按"结构化结果里的真实字符串"记录：JSON 序列化会把
            # 值内的换行/引号转义成别的形状，find_urls 在转义文本上记下的规范形
            # 与外发文本（真实字符）对不上，同一 URL 就会漏拦。
            record_tool_output(result)
            text = json.dumps(result, ensure_ascii=False, default=str)
        except ToolTimeoutError as exc:
            # 超时单独成支：给模型的话术要说明"这次没做成、且可能仍在后台跑完"，
            # 不能和"执行出错"混成一句，否则模型只会原样重试。
            message = f"工具 {name} 执行失败: {exc}"
            events.append(
                AgentEvent(
                    EventType.TOOL_RESULT,
                    {"tool": name, "ok": False, "error": message, "timed_out": True},
                )
            )
            return self._finalize_execution(
                events, name=name, call_id=call_id, started=started,
                key=idempotency_key, fingerprint=fingerprint,
                dependencies=trace[trace_start:],
                ok=False, text=message, status="unknown",
            )
        except Exception as exc:  # noqa: BLE001 - 工具错误必须回灌给模型而不是崩溃
            message = f"工具 {name} 执行出错: {type(exc).__name__}: {exc}"
            events.append(
                AgentEvent(EventType.TOOL_RESULT, {"tool": name, "ok": False,
                                                   "error": message})
            )
            return self._finalize_execution(
                events, name=name, call_id=call_id, started=started,
                key=idempotency_key, fingerprint=fingerprint,
                dependencies=trace[trace_start:],
                ok=False, text=message, status="unknown",
            )

        text, truncated, result_chars = self._bound(text)
        events.append(
            AgentEvent(
                EventType.TOOL_RESULT,
                {
                    "tool": name,
                    "ok": True,
                    "preview": event_preview(spec, result, text),
                    "truncated": truncated,
                    "result_chars": result_chars,
                },
            )
        )
        return self._finalize_execution(
            events, name=name, call_id=call_id, started=started,
            key=idempotency_key, fingerprint=fingerprint,
            dependencies=trace[trace_start:], ok=True, text=text, status="succeeded",
        )

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
            # 回收是内核替宿主做的动作，必须留痕：事件流是唯一出口，
            # 否则宿主只能从这句错误文案里反推"工具被收了"，
            # 也无法在事件流上区分"工具被回收"与"工具调用失败"。
            events.append(
                AgentEvent(
                    EventType.CODE_TOOL_RETIRED,
                    {"name": spec.name, "source": "code", "reason": "expired"},
                )
            )
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
        # 治理声明只能收紧沙箱超时：声明 10ms 时不能仍给沙箱默认的 10s。
        sandbox_timeout = self.code_timeout
        declared = spec.governance.timeout_ms
        if declared is not None:
            per_tool = declared / 1000.0
            sandbox_timeout = per_tool if sandbox_timeout is None else min(
                sandbox_timeout, per_tool
            )
        try:
            sb = await self.sandbox.execute(
                spec.code or "", inputs=arguments, timeout=sandbox_timeout
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
                    "preview": event_preview(spec, sb.output, text),
                    "truncated": truncated,
                    "result_chars": result_chars,
                },
            )
        )
        return events, True, text

    async def _execute_composite(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        subject: Any = None,
        decision: GateDecision | None = None,
        *,
        budget: Any = None,
        events: list[AgentEvent] | None = None,
        preview: ChangePreview | None = None,
        call_id: str = "",
        trace: list[_PermissionDependency],
    ) -> tuple[list[AgentEvent], bool, str]:
        """执行组合工具：逐步编排内部工具，每一步都递归走完整权限链路。

        组合工具本身不产生新原始能力，因此不再对组合名做一次权限检查
        （那会让用户看到一个黑盒名字）；权限精确落到每一步的内部工具上。

        例外：**组合工具自己声明了审批要求**（``gate`` 或 ``approval`` 抬出来的
        ``require_approval``）。那是宿主写明"这整条工作流开工前要问一次"，
        不是黑盒——所以这一步真的去问，绝不静默丢掉这份保护。问的时候只走
        :meth:`Channel.confirm`，不再跑一遍策略（策略对黑盒名字的判断仍然不做）。
        """
        # 由调用方传入同一个事件列表：工作流超时被取消时，已执行步骤的事件
        # 必须仍然可见——副作用发生了，审计链不能只剩一句"超时"。
        events = [] if events is None else events
        steps = spec.steps
        if not steps:
            return events, False, f"组合工具 {spec.name} 缺少步骤"
        # 声明超时不仅要包住整条工作流，还要下发到每个步骤：同步 handler 默认在
        # 事件循环线程里直接跑，外包的超时在它返回前根本没机会触发。
        declared = spec.governance.timeout_ms
        if declared is None:
            deadline = None
        else:
            deadline = asyncio.get_running_loop().time() + declared / 1000.0
        step_names = [step.tool for step in steps]
        # 对外副本按**组合工具自己的声明**脱敏（与 authorize 同一口径）：
        # 宿主在清单上写了 sensitive，就不能因为这条路走了组合分支而失效。
        # 执行用的 arguments 保持真值，脱敏只影响"别人看见什么"。
        shown = redact_arguments(
            arguments,
            spec.governance.sensitive,
            broad=spec.governance.audit_sensitive,
        )
        if decision is not None and decision.require_approval:
            asked = redact_arguments(arguments, spec.governance.sensitive)
            payload: dict[str, Any] = {
                "tool": spec.name,
                "arguments": asked,
                "steps": step_names,
                "approval_required": True,
                "reason": decision.reason,
            }
            if decision.prompt:
                payload["prompt"] = fill_prompt(decision.prompt, asked)
            for key, value in (
                ("risk", decision.risk),
                ("scope", decision.scope),
                ("target", decision.target),
            ):
                if value:
                    payload[key] = value
            preview_data = preview.to_dict() if preview is not None else None
            if preview_data:
                payload["preview"] = preview_data
            events.append(AgentEvent(EventType.PERMISSION_ASKED, payload))
            # 与 authorize_tool_call 同一个口径：通道声明了旗标才递第三个参数。
            shown_for_channel = redact_arguments(arguments, spec.governance.sensitive)
            context = approval_context(
                spec.name,
                require_approval=True,
                reason=decision.reason,
                prompt=payload.get("prompt", ""),
                risk=decision.risk,
                scope=decision.scope,
                target=decision.target,
                preview=preview_data,
            )
            approved = await confirm_with_context(
                self.channel, spec.name, shown_for_channel, context
            )
            if not approved:
                message = f"用户/宿主拒绝执行工具 {spec.name}"
                events.append(
                    self._audit.denied_event(
                        spec, spec.name, arguments, message, "approval"
                    )
                )
                return events, False, message
            if preview is not None:
                preview_failure, flags = await self._approved_preview_failure(
                    spec, arguments, subject, preview
                )
                if preview_failure is not None:
                    events.append(AgentEvent(
                        EventType.TOOL_RESULT,
                        {"tool": spec.name, "ok": False, "error": preview_failure, **flags},
                    ))
                    return events, False, preview_failure
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

            step_call_id = f"{call_id}:{index}" if call_id else ""
            step_timeout = None
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    assert declared is not None
                    message = _composite_timeout_message(spec.name, declared)
                    events.append(
                        AgentEvent(
                            EventType.TOOL_RESULT,
                            {
                                "tool": spec.name,
                                "ok": False,
                                "error": message,
                                "timed_out": True,
                            },
                        )
                    )
                    return events, False, message
                step_timeout = remaining
            try:
                sub_events, ok, sub_text = await self.execute(
                    step.tool,
                    resolved,
                    subject=subject,
                    budget=budget,
                    call_id=step_call_id,
                    _trace=trace,
                    _timeout=step_timeout,
                )
            except BudgetExceeded as exc:
                # 预算硬熔断要继续冒泡到 AgentLoop 统一收尾，但本组合已经密封的
                # 子收据不能丢：把已积累的事件挂到异常上（组合自身的事件在前，
                # 子步骤的部分事件在后），由 AgentLoop 在熔断收尾时补发，
                # 否则导出的收据链会因缺环而校验失败。
                exc.partial_events = events + list(exc.partial_events)
                raise
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
                    "preview": event_preview(spec, final, text),
                    "truncated": truncated,
                    "result_chars": result_chars,
                },
            )
        )
        return events, True, text
