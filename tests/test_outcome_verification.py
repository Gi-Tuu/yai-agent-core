"""Outcome Verification（v0.26 Batch 3）离线测试。

覆盖：
1. 证据 / 结论类型约束（状态枚举、置信度夹取、证据摘要截断）；
2. CompositeVerifier 的合并规则（失败优先 / 全成功 / 部分 / 未确认 / 不可判定）；
3. 从事件流收集证据（执行收据 + 工具显式 evidence）；
4. AgentCore 集成：DONE 事件与 RunResult 带验收结果，最终文本不受影响；
5. 验收器异常降级为 not_verifiable 并记诊断，不触发任何重试 / 重复副作用；
6. 浏览器动作的页面证据可被验收收集。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from yai_core import (
    AgentCore,
    CompositeVerifier,
    Evidence,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    VerificationResult,
    evidence_from_events,
    verify_run,
)
from yai_core.types import EVIDENCE_SUMMARY_MAX
from yai_core.verification import (
    EVIDENCE_MAX_ITEMS,
    EVIDENCE_TOTAL_MAX_CHARS,
    limit_verification_result,
)


def _run(coro):
    return asyncio.run(coro)


# ---------- 类型约束 ----------


def test_evidence_truncates_summary() -> None:
    evidence = Evidence(kind="receipt", summary="x" * 2000)
    assert len(evidence.summary) == EVIDENCE_SUMMARY_MAX


def test_evidence_requires_kind() -> None:
    with pytest.raises(ValueError):
        Evidence(kind="", summary="x")


def test_verification_status_validated() -> None:
    with pytest.raises(ValueError):
        VerificationResult(status="success")


def test_confidence_is_clamped() -> None:
    assert VerificationResult(status="unknown", confidence=5.0).confidence == 1.0
    assert VerificationResult(status="unknown", confidence=-1.0).confidence == 0.0


def test_roundtrip_serialization() -> None:
    original = VerificationResult(
        status="verified_success",
        goal="提交表单",
        confidence=0.8,
        reason="页面出现成功提示",
        evidence=(Evidence(kind="page", summary="成功提示", ok=True),),
    )
    assert VerificationResult.from_dict(original.to_dict()) == original


# ---------- CompositeVerifier ----------


class FakeVerifier:
    def __init__(
        self,
        result: VerificationResult | None = None,
        *,
        error: Exception | None = None,
        raw: Any = None,
    ) -> None:
        self.result = result
        self.error = error
        self.raw = raw
        self.calls = 0
        self.seen_goal: str = ""
        self.seen_evidence: list[Evidence] = []
        self.seen_metadata: dict[str, Any] = {}

    async def verify(self, goal, *, evidence=(), metadata=None):
        self.calls += 1
        self.seen_goal = goal
        self.seen_evidence = list(evidence)
        self.seen_metadata = dict(metadata or {})
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        if self.result is not None:
            return self.result
        return VerificationResult(status="unknown", goal=goal)


def _result(status: str, confidence: float = 0.5) -> VerificationResult:
    return VerificationResult(
        status=status,
        goal="g",
        confidence=confidence,
        evidence=(Evidence(kind="k", summary=status),),
    )


def test_composite_failure_wins() -> None:
    composite = CompositeVerifier(
        [FakeVerifier(_result("verified_success")), FakeVerifier(_result("verified_failure"))]
    )
    result = _run(composite.verify("g"))
    assert result.status == "verified_failure"


def test_composite_all_success() -> None:
    composite = CompositeVerifier(
        [
            FakeVerifier(_result("verified_success", 0.9)),
            FakeVerifier(_result("verified_success", 0.6)),
        ]
    )
    result = _run(composite.verify("g"))
    assert result.status == "verified_success"
    assert result.confidence == pytest.approx(0.6)  # 取更保守的置信度


def test_composite_success_plus_unknown_is_partial() -> None:
    composite = CompositeVerifier(
        [FakeVerifier(_result("verified_success")), FakeVerifier(_result("unknown"))]
    )
    assert _run(composite.verify("g")).status == "partial"


def test_composite_only_unknown() -> None:
    composite = CompositeVerifier([FakeVerifier(_result("unknown"))])
    assert _run(composite.verify("g")).status == "unknown"


def test_composite_empty_or_all_broken_is_not_verifiable() -> None:
    assert _run(CompositeVerifier([]).verify("g")).status == "not_verifiable"
    broken = CompositeVerifier(
        [FakeVerifier(error=RuntimeError("boom")), FakeVerifier(raw="not-a-result")]
    )
    result = _run(broken.verify("g"))
    assert result.status == "not_verifiable"
    assert "boom" in result.reason


def test_composite_dedupes_evidence() -> None:
    shared = Evidence(kind="page", summary="成功", ok=True)
    composite = CompositeVerifier(
        [
            FakeVerifier(VerificationResult(status="verified_success", evidence=(shared,))),
            FakeVerifier(VerificationResult(status="verified_success", evidence=(shared,))),
        ]
    )
    result = _run(composite.verify("g"))
    assert len(result.evidence) == 1


# ---------- VER-1：异常/非法返回分支按 not_verifiable 计入，不得冒充全成功 ----------


def test_composite_broken_branch_cannot_yield_verified_success() -> None:
    composite = CompositeVerifier(
        [
            FakeVerifier(_result("verified_success")),
            FakeVerifier(error=RuntimeError("state api down")),
        ]
    )
    result = _run(composite.verify("g"))
    # 一个分支成功、一个分支不可判定 → partial，而不是 verified_success。
    assert result.status == "partial"
    assert "state api down" in result.reason


def test_composite_invalid_branch_cannot_yield_verified_success() -> None:
    composite = CompositeVerifier(
        [FakeVerifier(_result("verified_success")), FakeVerifier(raw="not-a-result")]
    )
    result = _run(composite.verify("g"))
    assert result.status == "partial"
    assert "#1" in result.reason


def test_composite_failure_with_broken_branch_stays_failure_and_keeps_reasons() -> None:
    composite = CompositeVerifier(
        [
            FakeVerifier(_result("verified_failure")),
            FakeVerifier(error=RuntimeError("boom")),
        ]
    )
    result = _run(composite.verify("g"))
    assert result.status == "verified_failure"
    assert "boom" in result.reason


# ---------- 事件证据收集 ----------


def test_evidence_from_events_reads_receipts_and_explicit_evidence() -> None:
    from yai_core import AgentEvent

    events = [
        AgentEvent(
            "tool_result",
            {
                "tool": "submit",
                "receipt": {
                    "tool": "submit",
                    "status": "succeeded",
                    "retryable": False,
                    "postcondition": "confirmed",
                },
                "evidence": [
                    {"kind": "page", "summary": "出现成功提示", "ok": True},
                    {"bad": "shape"},  # 缺 kind → 跳过
                ],
            },
        ),
        AgentEvent("tool_result", {"tool": "x", "receipt": {"tool": "x", "status": "unknown"}}),
    ]

    collected = evidence_from_events(events)

    kinds = [item.kind for item in collected]
    assert kinds == ["receipt", "page", "receipt"]
    assert collected[0].ok is True
    assert collected[2].ok is None  # unknown 不冒充成功 / 失败


# ---------- AgentCore 集成 ----------


def _core_with_tool(verifier, *, handler=None) -> tuple[AgentCore, list[int]]:
    from yai_core import build_spec

    calls: list[int] = []

    def submit(value: str) -> str:
        calls.append(1)
        return "ok"

    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="c1", name="submit", arguments={"value": "x"})
                ],
            ),
            ModelResponse(content="表单已提交"),
        ]
    )
    core = AgentCore(model, outcome_verifier=verifier)
    core.register_tools([build_spec(submit)])
    return core, calls


def test_run_attaches_verification_and_keeps_final_text() -> None:
    verifier = FakeVerifier(
        VerificationResult(
            status="verified_success",
            goal="提交表单",
            confidence=0.9,
            reason="页面显示成功",
        )
    )
    core, calls = _core_with_tool(verifier)

    result = _run(core.run("提交表单"))

    assert result.final_text == "表单已提交"
    assert result.verification is not None
    assert result.verification.status == "verified_success"
    assert calls == [1]  # 只执行一次，验收不触发重复副作用
    done = [event for event in result.events if event.type == "done"]
    assert done[0].data["verification"]["status"] == "verified_success"
    # 证据来自本次 run 的执行收据
    assert any(item.kind == "receipt" for item in verifier.seen_evidence)
    assert verifier.seen_metadata["strategy"] == result.strategy.value


def test_verifier_exception_degrades_without_touching_result() -> None:
    diagnostics: list[Any] = []
    verifier = FakeVerifier(error=RuntimeError("state api down"))
    core, calls = _core_with_tool(verifier)
    core.on_diagnostic = diagnostics.append

    result = _run(core.run("提交表单"))

    assert result.final_text == "表单已提交"
    assert result.verification is not None
    assert result.verification.status == "not_verifiable"
    assert "state api down" in result.verification.reason
    assert calls == [1]
    assert [event.kind for event in diagnostics] == ["verification_failed"]


def test_no_verifier_keeps_backward_compatible_result() -> None:
    core, _calls = _core_with_tool(None)
    result = _run(core.run("提交表单"))
    assert result.verification is None
    done = [event for event in result.events if event.type == "done"]
    assert "verification" not in done[0].data


def test_unknown_verification_does_not_retry_tools() -> None:
    verifier = FakeVerifier(VerificationResult(status="unknown", goal="g"))
    core, calls = _core_with_tool(verifier)

    result = _run(core.run("提交表单"))

    assert result.verification is not None
    assert result.verification.status == "unknown"
    assert calls == [1]


# ---------- Browser 证据接入 ----------


def test_browser_page_evidence_enters_verification() -> None:
    from yai_core.channels import CollectChannel
    from yai_core.integrations.browser import BrowserProvider, build_browser_tools
    from yai_core.policy import AllowlistPolicy
    from yai_core.tools import ToolExecutor, ToolRegistry

    class _Backend:
        async def observe(self):
            from yai_core.integrations.browser import PageObservation

            return PageObservation(
                url="https://allowed.test/done",
                title="成功",
                text="提交成功",
                fingerprint="fp",
            )

    provider = BrowserProvider(_Backend())
    registry = ToolRegistry()
    registry.register_many(build_browser_tools(provider))
    executor = ToolExecutor(registry, AllowlistPolicy((), mode="auto"), CollectChannel())

    events, ok, _text = _run(executor.execute("browser_observe", {}))
    assert ok is True
    collected = evidence_from_events(events)
    assert any(item.kind == "page" for item in collected)


def test_verify_run_without_verifier_returns_none() -> None:
    assert _run(verify_run(None, "g", [])) is None


# ---------- VER-2：证据条数 / detail / 总量上限 ----------


def _collect_events_from_tool_results(count: int, *, big_detail: int = 0) -> list:
    """构造 ``count`` 条带显式 evidence 的 TOOL_RESULT 事件（离线、纯内存）。"""
    from yai_core import AgentEvent

    events = []
    for index in range(count):
        evidence_item: dict = {"kind": "page", "summary": f"页面 {index}", "ok": True}
        if big_detail:
            evidence_item["detail"] = {"blob": "x" * big_detail}
        events.append(
            AgentEvent(
                "tool_result",
                {"tool": "browser_observe", "evidence": [evidence_item]},
            )
        )
    return events


def test_evidence_item_count_is_capped_with_truncation_marker() -> None:
    from yai_core.verification import EVIDENCE_MAX_ITEMS

    collected = evidence_from_events(_collect_events_from_tool_results(80))

    assert len(collected) == EVIDENCE_MAX_ITEMS + 1  # 上限条 + 1 条截断标注
    marker = collected[-1]
    assert marker.kind == "truncated"
    assert "截断" in marker.summary
    assert marker.detail["candidates"] == 80
    assert marker.detail["dropped"] == 80 - EVIDENCE_MAX_ITEMS
    assert marker.detail["max_items"] == EVIDENCE_MAX_ITEMS


def test_evidence_oversized_detail_is_clipped_and_marked() -> None:
    from yai_core.verification import EVIDENCE_DETAIL_MAX_CHARS

    collected = evidence_from_events(
        _collect_events_from_tool_results(1, big_detail=EVIDENCE_DETAIL_MAX_CHARS * 3)
    )

    assert len(collected) == 2  # 证据 + 截断标注
    clipped = collected[0]
    assert clipped.detail["truncated"] is True
    assert clipped.detail["original_chars"] > EVIDENCE_DETAIL_MAX_CHARS
    assert len(clipped.detail["preview"]) == EVIDENCE_DETAIL_MAX_CHARS
    marker = collected[1]
    assert marker.kind == "truncated"
    assert marker.detail["detail_truncated"] is True


def test_evidence_total_size_is_capped() -> None:
    import json

    from yai_core.verification import EVIDENCE_TOTAL_MAX_CHARS

    # 每条 detail 3900 字符（未触发单条截断），累计必然超过 64KB 总量上限。
    collected = evidence_from_events(
        _collect_events_from_tool_results(40, big_detail=3900)
    )

    marker = collected[-1]
    assert marker.kind == "truncated"
    assert marker.detail["dropped"] > 0
    payload = json.dumps(
        [item.to_dict() for item in collected], ensure_ascii=False
    )
    # 截断后总量必须落在硬上限内（含标注本身）。
    assert len(payload) <= EVIDENCE_TOTAL_MAX_CHARS


def test_done_verification_payload_stays_bounded_on_huge_evidence() -> None:
    """端到端：超大证据经上限收敛后，DONE.verification 不会撑爆事件载荷。"""
    import json

    from yai_core.verification import EVIDENCE_TOTAL_MAX_CHARS

    events = _collect_events_from_tool_results(120, big_detail=3900)
    # 直接调用 verify_run 观察"交给验收器的证据"与"结论序列化体量"。
    captured: list = []

    class _EchoVerifier:
        async def verify(self, goal, *, evidence=(), metadata=None):
            captured.extend(evidence)
            return VerificationResult(
                status="unknown", goal=goal, evidence=tuple(evidence)
            )

    result = _run(verify_run(_EchoVerifier(), "g", events))
    assert result is not None
    assert captured and captured[-1].kind == "truncated"
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    assert len(payload) <= EVIDENCE_TOTAL_MAX_CHARS + 2_000


def test_host_verifier_oversized_evidence_is_capped_without_changing_status() -> None:
    """宿主 verifier 自返回的超大 evidence 也要在最终载荷前收紧，状态不被改写。"""
    big = tuple(
        Evidence(
            kind="host",
            summary=f"evidence-{index}",
            ok=True,
            detail={"index": index, "blob": "x" * 200},
        )
        for index in range(EVIDENCE_MAX_ITEMS * 3)
    )

    class _HugeVerifier:
        async def verify(self, goal, *, evidence=(), metadata=None):
            return VerificationResult(
                status="verified_success",
                goal=goal,
                confidence=0.9,
                reason="original",
                evidence=big,
            )

    result = _run(verify_run(_HugeVerifier(), "g", []))
    assert result is not None
    assert result.status == "verified_success"
    assert result.confidence == 0.9
    assert len(result.evidence) <= EVIDENCE_MAX_ITEMS + 1
    assert result.evidence[-1].kind == "truncated"
    assert result.reason == "original"
    assert len(str(result.to_dict())) <= EVIDENCE_TOTAL_MAX_CHARS + 2_000


def test_limit_verification_result_handles_non_json_detail() -> None:
    result = VerificationResult(
        status="not_verifiable",
        goal="g",
        evidence=(
            Evidence(
                kind="host",
                summary="non-json",
                detail={"object": object(), "nested": {"x": 1}},
            ),
        ),
    )
    limited = limit_verification_result(result)
    assert limited.status == "not_verifiable"
    assert limited.evidence[0].kind == "host"
