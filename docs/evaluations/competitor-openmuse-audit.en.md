> English translation of [中文](competitor-openmuse-audit.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# Competitor Source Verification: OpenMuse-Win (2026-11-03, read-only)

**English** · [简体中文](competitor-openmuse-audit.md)

> Repository: <https://github.com/wojiadexiaoming-copy/openmuse-win>
> Verified commit: `2385664a1c754b1e9a8a15a0f4a88d64b04ea4b8` (2026-10-29, no newer main)
> Method: read GitHub raw source and repository tree directly (no cloning, no execution, no modification of the competitor repository).
> Scope limit: **source-level verification**; no test was run in the competitor's environment, and "real-environment validation" is judged from
> their test markers and dependency lists and labelled "source evidence" rather than "our own live-test".

## 0. License and Facts

| Item | Value |
|---|---|
| License | **AGPL-3.0** (`license.spdx_id`) → incompatible with MIT-licensed YAI, **copying any code is prohibited** |
| Default branch / latest commit | `main` / `2385664` (2026-10-29) |
| Size | repository ~918 KB; `tools/browser/worker.py` 4,710 lines, `agent/orchestrator.py` 1,534 lines, `tools/desktop.py` 1,411 lines, `agent/llm_router.py` 838 lines, `agent/approval.py` 607 lines, `agent/verifier.py` 444 lines |
| Tests | **88 test files** (18+ browser-related, `test_completion_verifier.py` 346 lines, `test_desktop.py` 243 lines) |
| CI | **no `.github/` directory, no CI workflow of any kind** (repository tree measured 0 `.github/*` files) |
| Third-party dependencies (`requirements.txt`) | openai, playwright, pillow, apscheduler, sqlalchemy, qrcode, pywebview, pyautogui, pywinauto, httpx, pyyaml, pytest |

> Clarification of the "zero dependencies" wording: their `pyproject.toml` lists only three items — pyautogui / pywinauto / qrcode —
> while the full `requirements.txt` is the truth: **there is no Agent-framework dependency (LangChain/CrewAI/AutoGen),
> but the complete system depends heavily on third-party libraries**. YAI's `dependencies = []` is a stricter kernel constraint and
> does not mean "the whole product has zero dependencies".

## 1. Per-File Verification (source evidence)

| File | Volume | Real implementation | Simulated / unverified evidence | Worth noting |
|---|---|---|---|---|
| `tools/browser/worker.py` | 4,710 lines | Playwright page observation, DOM/accessibility extraction, click/type/wait/tab handling | In `tests/test_browser.py`: `@pytest.mark.skipif(True, reason="需 Chromium 真机…")` ("needs a real Chromium machine…"); real-browser cases are skipped by default (`no Chromium → pytest.skip`) | Browser capability is implemented as a **monolithic worker**, not separated from the kernel's governance contracts |
| `tools/desktop.py` | 1,411 lines | Window enumeration / screenshots / hotkeys / UIA operations (pywinauto + pyautogui + Pillow) | `tests/test_desktop.py` uses `MagicMock` + `fake_gui()` to simulate the desktop, **not real-machine UIA** | Windows coupled into the tool layer |
| `tools/desktop_accessibility.py` | 666 lines | UIA control Pattern queries (`mock_patterns` is only a variable name, not a fake implementation) | no real-machine test markers | Semantic UIA approach points the same way as YAI's Desktop Provider |
| `agent/llm_router.py` | 838 lines | multi-Provider routing, per-role failure recovery, capability probing (`supports_images`), credential tags, serialization | no corresponding "zero calls to unauthorized providers" test | there is an early form of routing governance, but no host declarative authorization set |
| `agent/approval.py` | 607 lines | **L1/L2/L3 three-tier approval** (in the source, L1×7 / L2×8 / L3×14), humanized approval summaries | unit tests only; no evidence of a real approval chain | the tiering idea is worth borrowing, but it is hard-coded in the application layer |
| `agent/risk.py` | 124 lines | command/path risk determination (shlex parsing) | unit tests | lightweight rules, not a pluggable policy |
| `agent/verifier.py` | 444 lines | **deterministic artifact/evidence checks**: `capture_artifacts`, `extract_criteria`, `check_criterion`, `verify_outcome`; no LLM/screenshot keyword matching | has `test_completion_verifier.py` (346 lines), skewed toward file/artifact assertions | same direction as YAI's Outcome Verification contract; YAI additionally separates execution receipts from task acceptance and supports host verifiers |
| `agent/result_observer.py` | 174 lines | **observation aid** for screenshot/DOM changes (`_evidence`, `observe`, `build_result_observer`) | no real-machine evidence found | vision is only an aid, not the sole acceptance basis (consistent with YAI's position) |
| `agent/orchestrator.py` | 1,534 lines | ReAct orchestration (openai calls, context, tool loop) | unit tests + no CI | monolithic orchestrator, tightly coupled to Windows/UI |
| `agent/subagent.py` | 347 lines | sub-Agent delegation (narrowed tools/context) | unit tests | similar in spirit to YAI's `shell/employee`, but coupled to the application layer |

## 2. Answers to Six Questions

1. **Which features really work?** At source level: the browser worker, desktop tools, multi-Provider routing,
   three-tier approval, deterministic artifact acceptance and the orchestrator all have substantive implementations (not empty shells).
2. **Which have only simulated tests?** Desktop UI automation — `test_desktop.py` uses `MagicMock`/`fake_gui` throughout;
   real browser-operation tests default to `skipif(True)` (needs real Chromium) and do not run in CI.
3. **Which have real-environment validation evidence?** There is **no** CI configuration in the repository; the tests contain branches
   "needs a real machine / skip if no Chromium", indicating the author did real-machine validation locally, but **the repository has no
   auditable automated evidence**.
4. **Architectural ideas worth borrowing:** ① L1/L2/L3 tiered approval; ② deterministic artifact acceptance (file/artifact checks)
   rather than model self-assessment; ③ UIA semantic Pattern first, geometric clicking as fallback; ④ multi-Provider capability probing and
   per-role failure recovery; ⑤ the result observer as an "evidence collection" side channel.
5. **Implementations unsuitable for YAI:** AGPL code cannot be copied; the orchestrator and tool layer tightly coupled to Windows/UI;
   making the browser worker a monolith rather than a pluggable execution adapter; an engineering chain without CI.
6. **YAI's equivalent mechanisms vs. real gaps:**

| Competitor capability | YAI current state | Conclusion |
|---|---|---|
| ReAct orchestration / planning / sub-Agents | `AgentLoop` + `AdaptiveRouter` + `shell/employee` | **equivalent already exists** |
| multi-Provider routing / failover | v0.26 `ProviderRoutingPolicy` + `allow_fallback` | **equivalent already exists and is stricter** (host authorization set, zero calls when unauthorized, sub-task narrowing) |
| L1/L2/L3 approval | native argument-level approval + `PermissionPolicy` + receipts | **equivalent already exists** (declaration-driven, not hard-coded) |
| browser automation | v0.26 Browser Provider (governance/snapshot references/receipts) + optional Playwright | **equivalent already exists**; real-Chromium smoke tests and controlled-task E2E are done (`docs/evaluations/browser-task-e2e.md`), real-site diversity is not covered (the competitor likewise lacks automated evidence) |
| Windows desktop automation | v0.26 Desktop Provider (UIA, focus protection, high-risk refusal) + local real-machine smoke test | **equivalent already exists**; coverage is narrower than the competitor (the competitor has more tools) |
| deterministic artifact acceptance | v0.26 `VerificationResult` + host verifier + browser/desktop evidence | **equivalent contract already exists**; the concrete domain verifiers must be implemented by the host |
| screenshot / visual localization | contract slot only (backend `screenshot()`, returns None by default) | **real gap** (not treated as a required capability; to be added on demand) |
| full Web UI / installer / scheduled tasks | none (we explicitly do not build UI/scheduling into the kernel) | **deliberately not done** (belongs to the host layer) |

## 3. Differentiators Provable by Objective Tests (YAI side)

1. **Zero calls to unauthorized Providers**: `tests/test_provider_routing.py` uses call counts to prove
   "when A is unauthorized, B is not called even if A fails", and that cancellation / timeout / retry do not change the authorization scope.
2. **Single governance path**: both browser and desktop tools register into `ToolRegistry` and produce receipts and events via
   `ToolExecutor`; no second execution/permission system exists.
3. **Memory corruption never silently loses files**: `tests/test_memory_vault_corruption.py` proves the three dispositions
   quarantine / read_only / raise plus byte preservation of the original file (the competitor has no corresponding evidence).
4. **Task acceptance separated from execution receipts**: `RunResult.verification` + the `DONE` event carry evidence;
   `tests/test_outcome_verification.py` proves that acceptance failure neither changes the text nor retries.
5. **Embeddability**: `dependencies = []` is guaranteed by an AST gate test, and browser/desktop/extension are all optional modules.

## 4. Conclusion

OpenMuse-Win is an AGPL Windows application with a **real implementation and a large test count, but no CI and real-environment
evidence only on the author's local machine**. YAI should not chase feature counts; the differentiation should rest on
**embeddable (MIT, zero hard dependencies) + governable (single execution path + host authorization) + verifiable (receipts/acceptance separated)
+ portable (optional Providers)**, proven with reproducible tests rather than screenshots/videos.

> Unverified items: the competitor's desktop-automation success rate on real Windows machines, browser task completion rate, and
> installer/product form — this round only verifies source and test structure, with no runtime comparison.
