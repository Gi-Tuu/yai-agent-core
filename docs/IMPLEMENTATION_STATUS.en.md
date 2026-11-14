> English translation of [中文](IMPLEMENTATION_STATUS.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# Capability and Evidence Index (IMPLEMENTATION_STATUS)

**English** · [简体中文](IMPLEMENTATION_STATUS.md)

> The README answers "what can it do"; this file answers **"how far has it actually got, where is the evidence,
> and where are the boundaries"**.
> Basis: **v0.36.0 is released (2026-11-11); v0.35.0 is the previous version**. The v0.26 / v0.27 / v0.28 batches
> (P0 security, Browser Provider, Outcome Verification, execution adapters) are marked inside this file;
> everything else follows the evidence column. Three status levels:
> **Verified** (offline tests or measured evidence) / **Implemented · not verified on real hardware** (the code is there, the real environment is missing) / **Not doing** (explicitly excluded).
>
> Self-check with one command: `python scripts/check_all.py` (full gate, offline)
> Machine-readable results of the two-host smoke: `python scripts/smoke_test.py --json`; stdout emits only one JSON object containing `ok` and per-host `checks`; exit 0 on success, exit 1 on failure.
> See the governance loop with one command: `python scripts/demo_governance_loop.py`
>
> **Measured at the current HEAD (before the v0.36.0 release)**: 2 451 items collected, 2 446 passed / 5 skipped,
> kernel line coverage 93.15% (gate 90%); the audit remediation items and verification boundaries are in
> [`docs/reports/audit-remediation-2026-11.md`](reports/audit-remediation-2026-11.md).

## 1. Kernel and execution

| Capability | Status | Evidence | Boundary |
|---|---|---|---|
| Adaptive routing (rules / LLM / online learning) | Verified | `tests/test_router.py`, `test_bandit_selector.py`, `test_route_outcomes.py`, `test_router_benchmark.py` | LLM classification needs a host-injected model; offline only the rule layer and the learning layer are verified |
| The four strategies direct / react / plan / clarify | Verified | `tests/test_loop.py`, `test_replan.py` | `strict_direct` must be enabled explicitly; the default direct path still executes structured tool_calls |
| Concurrency-serialization contract | Verified | `tests/test_core_concurrency.py` | Tasks on the same instance are serialized; for concurrency use multiple instances |
| Timeout and cancellation (model / tool / composite / sub-employee) | Verified | `tests/test_model_timeout.py`, `test_timeout_and_shutdown.py`, `test_shell_employee.py` | Synchronous handlers cannot be interrupted — only damage limitation is possible; on 3.11+ `asyncio.timeout` is used uniformly |
| Budget circuit breaking (three walls: model / tool / token) | Verified | `tests/test_budget.py` | Token counting depends on the backend returning usage; some backends do not |
| Composite tools (orchestrate registered tools only) | Verified | `tests/test_composite.py`, `test_composite_discovery.py` | Does not generate code, does not do visual orchestration |
| Idempotent replay + permission dependency re-check | Verified | `tests/test_execution_receipts.py` | Only takes effect for calls declared idempotent |
| Replanning within a task | Verified | `tests/test_replan.py` | Only carries tool pairings that have already completed |
| Task result acceptance contract (v0.26.0) | Verified (contract / composer / event integration) | `tests/test_outcome_verification.py` | The verifier is supplied by the host; when unconfigured `verification=None`; a failed acceptance degrades to `not_verifiable`, with no retry and no change to the final text |
| Provider routing governance (v0.26.0) | Verified (offline fake backends) | `tests/test_provider_routing.py`, `test_fallback_model.py` | Authorization declarations come from the host; unauthorized backends are called zero times; having no authorized target raises an explicit error; sub-tasks can only narrow |
| Prompt caching (Anthropic adapter layer) | Verified | `tests/test_anthropic_provider.py` (3 cases: default / disabled / breakpoint position) | Vendor-proprietary extension, only inside the adapter; compatible proxies can use `prompt_cache=False` |

## 2. Tool integration and discovery

| Capability | Status | Evidence | Boundary |
|---|---|---|---|
| MCP Registry discovery (v0.46) | Verified (offline parsing + live search) | `src/yai_core/integrations/mcp/registry.py`, `tests/test_mcp_registry.py` (9 cases, incl. allowlist/digest pin/signature hook), `examples/host_mcp_registry/` | Read-only search; no install/auto-connect; only `streamable-http` remotes become configs; registry schema is still v0 |
| A2A 1.0 positioning + host adapter (v0.46 / v0.47) | Verified (offline fake server + real tool call) | `docs/guides/a2a-positioning.en.md`, `examples/host_a2a_adapter/`, `tests/test_a2a_adapter.py` (11 cases, incl. SSE/OAuth flows/push), ADR 0008 | No A2A / Multi-Agent in the kernel; non-streaming + SSE + OAuth (client credentials / authorization code / device code) + push configs |
| Unified three sources: native functions / OpenAPI 3 (including 3.1) / MCP | Verified | `tests/test_openapi_bridge.py`, `test_openapi_31_compat.py` (3.1 / 2020-12 subset, 10 cases), `test_mcp_bridge.py` | MCP requires the `[mcp]` extra; 2020-12 only translates the tool-input subset, and constructs that are not translated are recorded as notes instead of rejecting the whole description ([compatibility matrix](guides/openapi-31-compat.en.md)) |
| Browser MCP extension end to end (v0.26, extension host) | Verified (real Edge on this machine; CDP mode included) | `scripts/register_native_host.py --browser-mcp playwright [--cdp-endpoint]` + the sidebar task "打开百度" ("Open Baidu"); CDP mode verified through Chrome launched by `scripts/chrome_cdp.py` (`/json/list` current page = baidu) | This is a host extension capability: MCP is optional and lazily loaded, and the kernel `dependencies` stay empty; CDP connects to the standalone-profile Chrome launched by chrome_cdp and cannot take over an existing default window |
| Local LLM configuration (v0.26, extension host) | Verified (panel entry + CLI + unit tests) | The sidebar "LLM 设置" (LLM settings) reads/saves (the key shows only its last 4 characters and is not stored in the browser); `scripts/configure_bridge_llm.py`; `tests/test_bridge_llm_config.py`, `tests/test_native_bridge.py` configure/get_config | The key is written only to the local bridge.json, and reading it back is always redacted; after saving you must reopen the sidebar for it to take effect; the CLI and the panel share the same config file |
| Capability-gap self-discovery + remote discovery sources | Verified | `tests/test_discovery*.py`, `test_remote_discovery.py` | Discovery is not authorization; it still goes through the unified policy |
| Capability manifest export / import | Verified | `tests/test_manifest.py` | `handler` and `gate` are functions; they are neither exported nor rebuilt from a snapshot |
| Strict argument validation | Verified | `tests/test_openai_compat.py`, `test_anthropic_provider.py` | Bad arguments in the historical context are only rendered, not forwarded |
| ACC declarations and OpenAPI binding | Verified | `tests/test_acc_*.py` (4 modules) | **Optional translator**: kernel-native governance is the primary path; ACC is not a dependency |
| Office text extraction (docx/xlsx/pptx with zero dependencies; PDF via the `[office]` extra pypdf) | Verified | `tests/test_office_text.py`, `tests/test_agentdojo_aggregate.py` | Text-level extraction: layout / styles / formulas / numeric cells do not enter the text layer; zip bombs and entity attacks are gated |
| Browser Provider (v0.26.0, optional adapter) | **Implemented · offline regression with a fake SDK + real-hardware smoke on this machine** | `tests/test_browser_provider.py` + `tests/test_playwright_backend_fake_sdk.py` (the fake playwright SDK covers start/observe/actions/tabs/wait/close/stale references); `scripts/browser_smoke.py` (real Chromium smoke on 2026-11-03: origin gate / input / dropdown / click / wait / re-observe / stale reference expiry, 11 receipts) | CI does not install a real browser, and the real backend still has no automated regression against real sites; nested frames, download/upload, CAPTCHA, real-site diversity, and visual grounding are not verified. `browser_tabs` now filters by the origin allowlist, and blocked tabs only return a count |

## 3. Governance and security

| Capability | Status | Evidence | Boundary |
|---|---|---|---|
| Permission policy (allowlist / custom SPI) | Verified | `tests/test_execution_policy.py`, `test_execution_boundaries.py` | Undeclared means unprotected — security semantics come only from declarations |
| Production-safe defaults (v0.25.0, B2) | Verified | `tests/test_production_defaults.py`, `examples/production_policy.py` | The default allow_all is unchanged but emits a ProductionSafetyWarning; for a production config template see docs/guides/production-permissions.md |
| Argument-level approval + approval context | Verified | `tests/test_native_approval.py`, `test_approval_context_channel.py` | A required approval cannot be bypassed by a policy denial |
| Capability gate (gate) | Verified | `tests/test_capability_gate.py` | Anything that cannot be judged fails closed |
| Resource scoping (file prefixes etc.) | Verified | `tests/test_resource_scope_gate.py`, `test_file_provider_security.py` | The scope gate takes effect before the handler |
| Trusted change preview + redaction | Verified | `tests/test_trusted_preview.py`, `test_redaction.py` | Previews are private data and are not sent to the model |
| Sub-employee permissions layered level by level | Verified | `tests/test_shell_employee.py` | Revocation at the parent takes effect immediately at run time |
| Sandbox contract | Verified (contract layer) | `tests/test_sandbox_spi.py`, `test_sandbox_static_gate.py` | The kernel only defines the contract and **does not bundle an executor** |
| Offline regression set for injection-style tool output | Verified | `tests/test_injection_regression.py` (policy denial / scope gate / approval denial + wrapping takes effect, 4 cases) | **Only tests the kernel defenses, not model robustness**; the model is scripted as the "worst case" |
| Tool output treated as untrusted data | Verified | `tests/test_loop.py` (two cases: default wrapping / can be disabled) | Only the prompt shape changes; the original values in events and receipts are unchanged |
| Post-execution verification (postconditions) | Verified | `tests/test_execution_receipts.py` (6 cases: confirmed / denied / check error / async / chain intact / not run for non-unknown) | Only applies to `unknown`; even if the check itself fails it is only recorded as `error`, never guessed on the host's behalf |
| Execution receipt hash chain | Verified | `tests/test_execution_receipts.py::test_receipt_chain_links_and_detects_tampering`; `verify_receipt_chain()` | Chaining is in-process; continuing the chain across processes is up to the host |
| Memory-at-rest corruption safety (v0.26.0) | Verified | `tests/test_memory_vault_corruption.py`, `tests/test_storage_cipher.py` | Classification (unreadable / corrupted / decryption failure / incompatible version / owner mismatch); three dispositions: quarantine / read_only / raise; decryption failure and incompatible version force read-only; the original file is never silently overwritten |
| Memory owner binding (v0.26.0, minimal isolation) | Verified | `tests/test_memory_vault_corruption.py` (owner_mismatch / same owner / legacy-file compatibility) | Full multi-tenancy is still up to the host via separate instances / separate paths; this item only prevents misreads and mistaken overwrites |
| Vault cross-process write lock (v0.27, fail-closed) | Verified | `tests/test_memory_vault.py` (two processes each writing 5 records, all kept; lock timeout **does not write to disk** / keeps dirty / diagnostics; flush retry; an unavailable lock file is not bypassed; a residual lock is harmless) | Not getting the lock within 2s = abandon this disk write, keep the in-memory changes, and retry on the next write or `flush()`; the advisory lock still does not constrain writes that bypass MemoryVault |
| Cost accounting and acceptance-evidence caps (2026-11 follow-up) | Verified | `tests/test_budget.py`, `tests/test_loop_runtime_contract.py`, `tests/test_outcome_verification.py`; every DONE path uses fixed keys, model_calls counts "attempts" and stays consistent with budget; evidence is truncated at 50 items / 4k per detail / 64KB total and marked | Evidence returned by the host verifier itself does not go through that cap (handled in v0.27 Batch 4); replan meta-tool counting has been consolidated into `_model_chat` |
| Outbound URL origin gate (v0.25.0) | Verified | `tests/test_egress_guard.py` | Declared paths are validated against the schema; declared protection without task context is denied; unknown URLs are not rejected for that reason, and no cross-task taint tracking is done |
| Outbound origin gate boundary hardening (v0.25.0, B1) | Verified | `tests/test_egress_guard.py` (Markdown/HTML/JSON wrapping, case and www variants, concurrent isolation, event fields) | A CJK character glued immediately after an ASCII hostname region is taken to terminate the hostname; path comparison is case-insensitive; IDNA lossy mappings keep a separate identity |
| Red line: kernel zero third-party hard dependencies | Verified | `tests/test_kernel_dependencies.py` (whole-repo AST scan; any injection turns it red) | Lazy loading inside a function body is still allowed |

## 4. Memory and context

| Capability | Status | Evidence | Boundary |
|---|---|---|---|
| Session memory + SQLite long-term memory | Verified | `tests/test_sqlite_memory.py`, `test_long_memory_e2e.py` | Storage is chosen by the host; the kernel does not force it |
| Backup / restore (staged validation + atomic replacement) | Verified | `tests/test_sqlite_backup.py` | Refuses the operation when sidecars (`-wal`/`-shm`) exist; does not provide multi-file crash atomicity |
| Summary compaction + hard-truncation fallback | Verified | `tests/test_context_compaction.py`, `test_context_compact.py` | If the summary times out or fails, it falls back to hard truncation |
| Storage encryption | Verified | `tests/test_storage_cipher.py` | Encryption/decryption goes through the SPI; unencrypted by default |
| Retention policy / TTL | Verified | `tests/test_memory_retention.py`, `test_kv_ttl.py` | Executed according to host configuration |
| Parallel summary compaction | Verified | `tests/test_context_compaction.py` (segments fill evenly / segment head alignment / concurrent wall clock / event fields) | Only affects wall clock; retention policy and message structure are unchanged; default 1 |
| Task anchor (context-rot protection) | Verified | `tests/test_context_compaction.py` (two cases) | Only injected when the original task is no longer in context, so tokens are not spent twice |
| Embedding recall (optional) | Verified (fake implementation) | `tests/test_integrations_embedding.py` | The local ONNX real-machine case needs its own model files; skipped in CI |
| Memory quality benchmark and 10k scale (v0.27 scale-up / v0.31 batch writes) | Verified (offline scripts + regression) | `benchmarks/memory/run_bench.py`, `scale_bench.py`, `tests/test_memory_batch.py`, `test_memory_metrics.py`, `test_memory_scale.py`; 114 records / 60 queries (real BGE-M3 Recall@5 0.899 / MRR@5 0.896 / P50 28.9ms); **10,000 records batch-built in 45.4s, retrieval P50 17.6ms / P95 116ms** (lexical only, on this machine) | The semantic channel is not benchmarked at 10k scale; record-by-record `remember` (full disk write per record) is still slow; for very large / high-frequency scenarios `SqliteStore` or a host vector store is still recommended; see [docs/evaluations/memory-evaluation.md](evaluations/memory-evaluation.md) |

## 5. Hosts and protocols

| Capability | Status | Evidence | Boundary |
|---|---|---|---|
| 11 example hosts (including 1 production-shaped one) + a native desktop shell (including cross-core call visualization, v0.35) | Verified | `tests/test_host_*.py`, `tests/test_host_production.py`, `tests/test_desktop_*.py` | The desktop shell is verified on real Windows hardware only; real third-party application behavior is still bounded by each example's own fake-backend tests |
| Directed parent-child cores Phase 1–3 + trailing items (v0.28–v0.33) | Verified (32 regression cases; two-process, process hosting, specialist bridge, and usage budget actually run) | `tests/test_federation.py` (12) + `test_federation_ipc.py` (7) + `test_federation_ops.py` (8: manifest hot update / per-edge quota and statistics / sub-core deregistration / process hosting and death fail-closed) + `test_federation_specialists.py` (5: specialist object introspection / attaching multiple specialists / usage aggregation and token budget / two installable sub-core demos); `examples/host_m_federation/`, `examples/child_cores/` | **This machine only**: cross-core transactions / guardian-style automatic restart / server-push manifest are not implemented; the specialist bridge is headless (wiring the Qt panel is on the host side); no peer-to-peer Multi-Agent; zero kernel changes |
| Windows Desktop Provider (v0.27, optional adapter) | **Verified (offline regression with a fake SDK + real charmap application 12/12)** | `tests/test_desktop_provider.py` + `tests/test_pywinauto_backend_fake_sdk.py` (a fake UIA tree covering observe/Invoke/SetValue/coordinate click/stale reference/process image resolution); `scripts/windows_uia_smoke.py` (self-built WinForms on 2026-11-03 → Invoke → callback verification) | Other real applications, screenshots, and UAC elevated windows are not accepted yet; `desktop_list_windows` filters by **process identity** (exact PID match / image name / full path) plus title, and blocked windows only return a count; for real-application verification see [docs/evaluations/windows-real-apps.md](evaluations/windows-real-apps.md) (character map 12/12, including dropdown selection; third-party apps / UAC / multiple monitors not covered) |
| Real-hardware nightly regression (since v0.34, non-blocking) | Verified (three scripts re-run on this machine + two manual triggers of the hosted runner) | `.github/workflows/nightly-real.yml` (schedule + workflow_dispatch); on this machine 2026-11-10: UIA `ok`, charmap `12/12`, browser `11 receipts`; hosted runner (run 42782702561): browser smoke + task E2E all green, the two Windows scripts honestly returned failure because there is **no interactive desktop** (artifacts kept as evidence, not disguised as passing) | The hosted Windows runner has no interactive desktop, and the two UIA steps use `continue-on-error` (check the artifact for results); does not cover real-site diversity / CAPTCHA / UAC / multiple monitors; a scheduled-task failure does not block PRs |
| Shared retry for atomic writes (v0.27) | Verified (unit tests) | `yai_core.fsutil.replace_with_retry`; shared by Vault/Bridge | May still fail in the end if the file stays occupied for a long time; a failure keeps dirty/diagnostics |
| Adapter secure defaults (v0.27 Batch 2 / v0.27.1 desktop tightening / v0.27.2 browser alignment) | Verified (offline + local HTTP + real Playwright redirect) | `tests/test_adapter_security_defaults.py`, `tests/test_desktop_policy_identity.py`, `tests/test_browser_redirect_guard.py`; `BrowserPolicy.secure/development`, `DesktopPolicy.secure/development`; omitting it, or passing a loose policy, both emit a Warning (browser: origin allowlist empty or `*`, recognized via `BrowserPolicy.origins_unrestricted`; desktop: no process identity); desktop process identity has three allowlists (exact PID / image name / path); file:/javascript:/data:/about: are denied by default; the Playwright routing layer aborts unauthorized document redirects | The full security matrix for real sites / real third-party applications still needs manual acceptance; control-name keywords and window titles are not security boundaries |
| Browser execution path unification ADR + controlled-browser E2E (v0.27 Batch 3) | Decided (ADR 0006, C coexists + explicit boundaries); the controlled end to end is verified | [docs/adr/0006-browser-execution-unification.md](adr/0006-browser-execution-unification.md); `scripts/browser_provider_task_smoke.py` (real Chromium: open/observe/type/reobserve/click/observe, 3 approvals, receipt chain intact, page text verified as verified_success); [docs/evaluations/browser-task-e2e.md](evaluations/browser-task-e2e.md); panel read-only/controlled distinction | Controlled-browser E2E has passed; current-tab write operations are implemented as an opt-in `current_tab_action` (off by default, protocol-level regression), but a real Side Panel user-gesture E2E still needs manual acceptance; it must not be claimed that current-tab write operations have passed on real hardware |
| Windows install/distribution (v0.27 Batch 6) | Verified (portable package + one-click install/uninstall) | `packaging/windows/installer.py`, `build_bundle.py`, `README.md`; actual install/uninstall/reinstall on this machine passed; artifact `dist/yai-agent-core-windows-0.26.0.zip` (about 46.6MB) | Not MSI/store distribution; requires Python 3.11+ and `yai-agent-core` on the target machine; the extension still has to be loaded manually; no auto-update (since v0.27.0 the installer includes upgrade snapshot / rollback); the zip file name inside `dist/` is the version at build time |
| Local key storage plan (v0.27 Batch 4) | Plan decided, encryption not implemented | [docs/adr/0007-local-secret-storage.md](adr/0007-local-secret-storage.md); the plaintext risk of `bridge.json`/`.bak` plus ACLs, rotation, and a SecretStore SPI draft | Still written to disk in plaintext; must not be advertised as encrypted storage; DPAPI/Credential Manager are selected as follow-up optional modules |
| Two real open-source applications integrated with zero modification | Verified | `tests/test_host_i_mealie.py`, `test_host_j_actual.py` | Runs against fake backends; does not connect to real instances |
| Sidecar protocol (stdio NDJSON + JSON-RPC 2.0) | Verified (Python protocol + typed TS client) | `tests/test_sidecar_protocol.py`, `test_sidecar_server.py`; `clients/node/test/` (5 cases, fake sidecar), `.github/workflows/client-node.yml` | Only carries the session surface; the tool surface still goes through MCP / OpenAPI; the TS client only covers the session surface |
| Native Messaging Bridge (v0.26.0) | **Verified (real process + real Sidecar + real Edge end to end)** | `scripts/register_native_host.py` (HKCU registration, reversible); `scripts/bridge_smoke.py --executable` (frozen exe + unauthorized exit code 2 + hello/status/shutdown); the real Edge sidebar shows "protocol handshake complete (bridge v1)", and `bridge.log` records frozen=True / exit ok | No end-to-end run on branded Chrome (same registry / same exe, already proven on Edge); no real task has been run (needs a model-credential environment); the Bridge is bound to one extension connection and provides no cross-process task recovery |
| Bridge config lock (v0.27, fail-closed) | Verified | `tests/test_bridge_llm_config.py` (two processes changing different fields without loss; read-modify-write refused when the lock times out; a residual lock is reused, not deleted) | OS-level cross-process lock (msvcrt/fcntl); a busy lock returns `config_lock_busy`; atomic replacement + `.bak` and key redaction are unchanged |
| Browser extension host (v0.26.0, MV3 Side Panel) | **Verified: loading / side panel / click behavior / real task + real-hardware state recovery** | `scripts/extension_smoke.py` (MV3 SW, sidepanel.html, `openPanelOnActionClick=true`); `tests/test_extension_assets.py` static contract; the real Edge end-to-end task "打开百度" ("Open Baidu") returns the title via Bridge→Sidecar→MCP; **real-hardware acceptance on 2026-11-03: closing and reopening the panel while running restores "running" and allows cancellation; after force-killing the Sidecar the SW reconnects automatically and a new task succeeds; after closing the last panel the Bridge restarts and re-reads the config** (screenshots `docs/exports/accept-reopen-cancel.png`, `accept-reopen-idle.png`, local artifacts) | Real mouse clicks on the toolbar icon and installer-package distribution are still not automated; multi-tab / download / upload / CAPTCHA on real sites are not verified |
| Host real-time state | Verified | `tests/test_host_state_provider.py` | State is an enhancement; failures only degrade |
| Channels (CLI / collection / desktop / sidecar) | Verified | `tests/test_cli_channel.py`, `test_sidecar_server.py` | Approval and clarification must go back to the host through a channel |

## 6. Observability and operations

| Capability | Status | Evidence | Boundary |
|---|---|---|---|
| 21 classes of structured events, single exit | Verified | `tests/test_spi_surface.py` (including the "no dead events" assertion) | Events are the only outward stream |
| Local diagnostics callback | Verified | `tests/test_diagnostics.py` | Errors thrown by the callback itself are swallowed and never damage the task |
| GenAI semantic-convention attributes (agent / tool) | Verified | `tests/test_tracing_otel.py` | Only stable fields are emitted; usage / model are not fabricated |
| OpenTelemetry tracing / metrics | Verified (fake exporters) | `tests/test_tracing_otel.py`, `test_metrics_recorder.py` | Requires the `[otel]` extra |
| Readiness/metrics HTTP endpoints | Verified | `tests/test_readiness_metrics_api.py`, `test_battery_api.py` | Part of batteries, not required by the kernel |

## 7. Packaging, supply chain, and release

| Capability | Status | Evidence | Boundary |
|---|---|---|---|
| Version strings consistent (pyproject / `__version__` / lock / both READMEs) | Verified | `tests/test_version_consistency.py` | Must be run for every release |
| sdist link integrity + tests runnable inside the package | Verified | `tests/test_sdist_link_integrity.py` (19 cases); CI `sdist` job builds → unpacks → pytest inside the package; re-verified on this machine 2026-11-03: `uv build --sdist` → unpack → full pytest inside the package, exit 0 | Only ships tracked public content |
| Container image publishing (GHCR) | Verified (PR build + Dockerfile HEALTHCHECK; the local Docker daemon was not started, so no local re-verification) | `.github/workflows/docker.yml`, `Dockerfile` (base image digest pinned); tags `v*` automatically publish semver + latest (provenance + SBOM) | Publishes on tag push only; does not include `[desktop]` / `[browser]` / `[windows]` dependencies; image size has not been optimized with a multi-stage build; Docker was not run on this machine |

| SLSA provenance + cosign signatures | Verified | v0.25.0 Release, 15 assets, including `slsa-provenance.intoto.jsonl` | Third-party scan recognition has a delay |
| OpenSSF badges / Scorecard | External score | CII Passing (#15360); Scorecard 7.7/10 (re-scanned 2026-10-28, Signed-Releases 9/10) | Subject to third-party re-scans |

## 8. Explicitly not doing

MCP Server · Multi-Agent orchestration · built-in vector database · built-in UI (the desktop shell lives in `shell/`, not in the kernel) ·
sandboxed execution of code-generation tools · coding agent · cross-instance distributed rate limiting · gVisor / micro-VM level isolation.
The rationale and tradeoffs are in the "what we are not doing" section of `ROADMAP.md`.

## 9. Known boundaries (what has not been verified — we do not pretend it has been)

- **Real models are only spot-checked**: CI uses scripted models throughout and depends on neither the network nor API keys.
- **Playwright backend**: the offline regression with the fake playwright SDK covers backend start/stop/observe/actions/tabs/wait/
  stale references (in CI); additionally `scripts/browser_smoke.py` ran one real Chromium/system Chrome smoke
  (navigate / observe / type / dropdown / click / wait / re-observe + origin gate + stale reference expiry, 11 receipts);
  it still does not cover nested frames, download / upload, CAPTCHA, real-site diversity, or visual grounding.
- **Browser extension**: on real Edge we have verified loading, the side panel, approval/clarification, a real task,
  cancelling by reopening the panel while running, automatic reconnection after force-killing the Sidecar, and the Bridge
  restarting and re-reading its config after the last panel closes (see the real-hardware acceptance record in
  [docs/guides/browser-extension-bridge.md](guides/browser-extension-bridge.md)); **real mouse clicks on the toolbar icon and installer-package
  distribution** still need manual acceptance, and "loaded successfully" must not be taken as "the distribution path is verified".
- **Windows Desktop Provider**: the offline regression with the fake pywinauto SDK covers observe/Invoke/SetValue/
  coordinate click/stale reference (in CI); additionally one UIA smoke on a self-built WinForms window was completed on this machine
  (observe / semantic Invoke / callback verification). For real third-party applications, complex control Patterns, screenshots,
  multiple monitors, and UAC elevated windows there is **no** verification, so "general desktop automation is verified" cannot be claimed on that basis.
- **The public demo instance is shut down**: the `yai-agent-core` service on Render was deleted on 2026-11-02; any future public deployment should
  explicitly configure `YAI_API_KEY` (see ADR 0002 D3).
- **The Windows symlink race** can only be narrowed, not eliminated (POSIX has `O_NOFOLLOW`); the corresponding tests are skipped on Windows.
- **The desktop shell is verified on real Windows hardware only**; the 60fps figure comes from a single sample at 2560×1600 / dpr 1.5.
- **The demo video was recorded on an older version**; the UI differs from the current desktop shell (the capability points are the same).
- **Static-scan hint (exec / sandbox)**: the repository contains two `exec()` occurrences, in
  `examples/host_g_sandbox/_worker.py` (a teaching sandbox) and `shell/desktop/_sandbox_worker.py`
  (the desktop shell's `SubprocessSandbox` implementation); both are **host-side one-shot subprocesses** (launched with `-I -S` + an
  AST gate before exec + restricted `__builtins__`), not kernel capabilities; `src/yai_core/` contains **no `exec` / `eval` at all**,
  and the kernel only defines the `ToolSandbox` contract without bundling an executor. When a third-party static scan flags these two spots as
  "dangerous code", read it against this boundary, and do not take it as the kernel running code.
