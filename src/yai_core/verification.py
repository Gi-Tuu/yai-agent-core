"""任务结果验收（Outcome Verification）契约与组合器（v0.26 Batch 3）。

核心区分（任务书 §7.2）：**工具调用成功** ≠ **副作用状态** ≠ **可观察结果**
≠ **目标达成**。执行收据（:class:`~yai_core.types.ExecutionReceipt`）回答前两者，
本模块回答后两者：

- 宿主实现 :class:`OutcomeVerifier`（可以是 API 二次查询、文件检查、DOM 断言……
  也可以是组合多个 verifier）；
- 内核在一次 run 结束时收集证据（收据 + 工具显式上报的 evidence），交给 verifier，
  得到 :class:`~yai_core.types.VerificationResult`，附在 ``DONE`` 事件与
  :class:`~yai_core.types.RunResult` 上；
- verifier 缺失 = ``None``（保持旧行为，不假装验收过）；verifier 抛错 = ``not_verifiable``
  并记诊断，绝不把异常当成功。

视觉模型只能作为可选补充：契约不要求也不依赖任何模型判断。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from yai_core.diagnostics import (
    DiagnosticEvent,
    DiagnosticKind,
    DiagnosticLevel,
    DiagnosticSink,
    emit,
)
from yai_core.types import (
    AgentEvent,
    EventType,
    Evidence,
    VerificationResult,
)


@runtime_checkable
class OutcomeVerifier(Protocol):
    """宿主提供的任务验收器；实现必须是 ``async``（可能需要 IO）。"""

    async def verify(
        self,
        goal: str,
        *,
        evidence: Sequence[Evidence] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> VerificationResult:
        ...


class CompositeVerifier:
    """把多个 verifier 的结果按"最坏情况优先"合成一个结论。

    规则（确定、可解释，不引入模型判断）：
    1. 任一 ``verified_failure`` → ``verified_failure``；
    2. **全部**分支 ``verified_success`` → ``verified_success``（抛异常/返回非法
       对象的分支按 ``not_verifiable`` 计入结果，不能被"跳过"成成功）；
    3. 有成功也有未确认 / 部分 → ``partial``；
    4. 只有 ``unknown`` → ``unknown``；
    5. 没有任何有效结论（含空列表、全部抛错、全部 ``not_verifiable``）→
       ``not_verifiable``。

    单个 verifier 抛错 / 返回非法对象不会中断整次验收：该分支记 ``not_verifiable``
    并**计入结果集**，理由里汇总写明（不能把异常当成功）。
    """

    def __init__(self, verifiers: Sequence[OutcomeVerifier]) -> None:
        self.verifiers = tuple(verifiers)

    async def verify(
        self,
        goal: str,
        *,
        evidence: Sequence[Evidence] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> VerificationResult:
        results: list[VerificationResult] = []
        failures: list[str] = []
        for index, verifier in enumerate(self.verifiers):
            try:
                result = await verifier.verify(
                    goal, evidence=evidence, metadata=metadata
                )
            except Exception as exc:  # noqa: BLE001 - 验收失败不能反噬主流程
                failures.append(f"#{index} {type(exc).__name__}: {exc}")
                results.append(
                    VerificationResult(
                        status="not_verifiable",
                        goal=goal,
                        reason=f"验收器 #{index} 抛异常（{type(exc).__name__}）",
                    )
                )
                continue
            if not isinstance(result, VerificationResult):
                failures.append(f"#{index} 返回了非 VerificationResult 对象")
                results.append(
                    VerificationResult(
                        status="not_verifiable",
                        goal=goal,
                        reason=(
                            f"验收器 #{index} 返回了 {type(result).__name__}，"
                            "不是 VerificationResult"
                        ),
                    )
                )
                continue
            results.append(result)

        merged_evidence = _dedupe_evidence(
            [item for result in results for item in result.evidence]
        )
        if not results:
            return VerificationResult(
                status="not_verifiable",
                goal=goal,
                reason="没有可用的验收器",
            )
        failure = next(
            (item for item in results if item.status == "verified_failure"), None
        )
        if failure is not None:
            return VerificationResult(
                status="verified_failure",
                goal=goal,
                confidence=max(item.confidence for item in results),
                reason=_merge_reason(
                    failure.reason or "至少一个验收器确认目标未达成", failures
                ),
                evidence=merged_evidence,
            )
        successes = [item for item in results if item.status == "verified_success"]
        if len(successes) == len(results):
            # 这里要求**全部**分支成功：异常/非法返回的分支已作为
            # not_verifiable 条目计入 results，不会被漏掉。
            return VerificationResult(
                status="verified_success",
                goal=goal,
                confidence=min(item.confidence for item in results),
                reason=results[0].reason if results else "",
                evidence=merged_evidence,
            )
        if successes or any(item.status == "partial" for item in results):
            return VerificationResult(
                status="partial",
                goal=goal,
                confidence=max(item.confidence for item in results),
                reason=_merge_reason("部分验收器确认达成、部分未确认", failures),
                evidence=merged_evidence,
            )
        if any(item.status == "unknown" for item in results):
            return VerificationResult(
                status="unknown",
                goal=goal,
                confidence=max(item.confidence for item in results),
                reason=_merge_reason("验收器无法确认结果", failures),
                evidence=merged_evidence,
            )
        return VerificationResult(
            status="not_verifiable",
            goal=goal,
            reason=_merge_reason("所有验收器均不可判定", failures),
            evidence=merged_evidence,
        )


def _merge_reason(reason: str, failures: Sequence[str]) -> str:
    """在结论理由后附上"哪些分支不可判定"，不让异常分支被静默吞掉。"""
    if not failures:
        return reason
    return f"{reason}（不可判定分支：{'；'.join(failures)}）"


def _dedupe_evidence(items: Sequence[Evidence]) -> tuple[Evidence, ...]:
    seen: set[tuple[str, str, bool | None]] = set()
    out: list[Evidence] = []
    for item in items:
        key = (item.kind, item.summary, item.ok)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return tuple(out)


def evidence_from_events(events: Sequence[AgentEvent]) -> list[Evidence]:
    """从一次 run 的事件流里收集验收证据。

    两种来源：
    1. 每个 ``TOOL_RESULT`` 的执行收据 → ``kind="receipt"``（成功 / 失败 / unknown）；
    2. 工具结果里显式携带的 ``evidence`` 列表（如浏览器动作上报的页面状态），
       逐条转成 :class:`Evidence`；形状不合法就跳过，不让脏数据打断验收。
    """
    out: list[Evidence] = []
    for event in events:
        if event.type != EventType.TOOL_RESULT:
            continue
        data = event.data or {}
        receipt = data.get("receipt")
        if isinstance(receipt, dict):
            tool = str(receipt.get("tool", ""))
            status = str(receipt.get("status", ""))
            ok: bool | None
            if status == "succeeded":
                ok = True
            elif status == "failed":
                ok = False
            else:
                ok = None
            out.append(
                Evidence(
                    kind="receipt",
                    summary=f"{tool}: {status}",
                    ok=ok,
                    detail={
                        "tool": tool,
                        "status": status,
                        "retryable": bool(receipt.get("retryable")),
                        "postcondition": str(receipt.get("postcondition", "")),
                    },
                )
            )
        extra = data.get("evidence")
        if isinstance(extra, list):
            for item in extra:
                if not isinstance(item, Mapping):
                    continue
                try:
                    out.append(Evidence.from_dict(item))
                except (TypeError, ValueError):
                    continue
    return out


async def verify_run(
    verifier: OutcomeVerifier | None,
    goal: str,
    events: Sequence[AgentEvent],
    *,
    metadata: Mapping[str, Any] | None = None,
    on_diagnostic: DiagnosticSink | None = None,
) -> VerificationResult | None:
    """运行验收器并返回结果；未配置返回 None，异常降级为 ``not_verifiable``。

    这里只做"旁路"：任何异常都被收口成 ``not_verifiable`` + 诊断，绝不改变本次
    任务的最终文本与事件流结构，也不会触发任何重试 / 重复副作用。
    """
    if verifier is None:
        return None
    evidence = evidence_from_events(events)
    try:
        result = await verifier.verify(goal, evidence=evidence, metadata=metadata or {})
    except Exception as exc:  # noqa: BLE001 - 验收是旁路，不能反噬主流程
        emit(
            on_diagnostic,
            DiagnosticEvent(
                kind=str(DiagnosticKind.VERIFICATION_FAILED),
                level=DiagnosticLevel.ERROR,
                message=f"任务验收器异常，降级为 not_verifiable：{type(exc).__name__}",
                data={"goal": goal[:200], "error": f"{type(exc).__name__}: {exc}"},
            ),
        )
        return VerificationResult(
            status="not_verifiable",
            goal=goal,
            reason=f"验收器异常：{type(exc).__name__}: {exc}",
        )
    if not isinstance(result, VerificationResult):
        emit(
            on_diagnostic,
            DiagnosticEvent(
                kind=str(DiagnosticKind.VERIFICATION_FAILED),
                level=DiagnosticLevel.ERROR,
                message="任务验收器返回了非法对象，降级为 not_verifiable",
                data={"goal": goal[:200], "type": type(result).__name__},
            ),
        )
        return VerificationResult(
            status="not_verifiable",
            goal=goal,
            reason=f"验收器返回了 {type(result).__name__}，不是 VerificationResult",
        )
    return result
