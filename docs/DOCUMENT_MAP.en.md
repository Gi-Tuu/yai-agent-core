# Document Map (English)

> English translation of [中文](DOCUMENT_MAP.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

> Answers "which documents exist, what each owns, and which one is authoritative". For capability
> evidence see [`IMPLEMENTATION_STATUS.en.md`](IMPLEMENTATION_STATUS.en.md); for a code-reading route
> see [`reading-guide.en.md`](reading-guide.en.md) and [`walkthrough/00-index.md`](walkthrough/00-index.md)
> (walkthrough is Chinese-only by policy).
>
> Online site: https://gi-tuu.github.io/yai-agent-core/ — built from `mkdocs.yml` and published to
> GitHub Pages. New public docs must be added to nav or `not_in_nav`; link checks live in
> `tests/test_docs_site.py` and `tests/test_sdist_link_integrity.py`.

## 1. Root front door (Chinese/English pairs; keep facts in sync)

| Document | Language | Purpose |
|---|---|---|
| [README.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/README.md) / [README.en.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/README.en.md) | zh / en | Project front door: positioning, 30-second start, capabilities, host examples |
| [CONTRIBUTING.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CONTRIBUTING.md) / [CONTRIBUTING.en.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CONTRIBUTING.en.md) | zh / en | Contribution flow and maintainer runbook (releases, gates) |
| [SECURITY.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/SECURITY.md) / [SECURITY.zh-CN.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/SECURITY.zh-CN.md) | en / zh | Security policy: supported versions and reporting channel |
| [CHANGELOG.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CHANGELOG.md) / [CHANGELOG.en.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CHANGELOG.en.md) | zh / en | Per-version ledger (Chinese canonical; English is a version-level summary) |
| [ROADMAP.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/ROADMAP.md) / [ROADMAP.en.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/ROADMAP.en.md) | zh / en | Direction and boundaries: shipped versions, candidates, explicit non-goals |

## 2. `docs/` root: architecture and positioning

| Document | Purpose |
|---|---|
| [architecture.md](architecture.md) / [architecture.en.md](architecture.en.md) | Architecture overview: layers, data flow, SPI, governance |
| [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) / [IMPLEMENTATION_STATUS.en.md](IMPLEMENTATION_STATUS.en.md) | Capability and evidence index: verified / implemented-not-real-machine / explicitly not doing |
| [whitepaper.md](whitepaper.md) / [whitepaper.en.md](whitepaper.en.md) | Technical whitepaper (v0.25.0 snapshot; current numbers in IMPLEMENTATION_STATUS) |
| [comparison.md](comparison.md) / [comparison.en.md](comparison.en.md) | Honest comparison with five categories of adjacent products (no name-calling) |
| [reading-guide.md](reading-guide.md) / [reading-guide.en.md](reading-guide.en.md) | Code-reading route (7 stops + exercises) |
| [BILINGUAL.md](BILINGUAL.md) | Bilingual coverage and policy: what has an English version, what stays Chinese-only |
| DOCUMENT_MAP.md / DOCUMENT_MAP.en.md | This map |

## 3. `docs/evaluations/` — evaluations and evidence (8)

| Document | Purpose |
|---|---|
| [agentdojo-evaluation.md](evaluations/agentdojo-evaluation.md) | AgentDojo prompt-injection evaluation (0.24.0 snapshot; injection-guard results, rerun) |
| [memory-evaluation.md](evaluations/memory-evaluation.md) | Memory retrieval quality: BGE-M3, 10k scale, LoCoMo lexical/semantic/end-to-end/judge |
| [browser-task-e2e.md](evaluations/browser-task-e2e.md) | Controlled-browser real-task E2E (real Chromium, receipt chain) |
| [windows-real-apps.md](evaluations/windows-real-apps.md) | Windows Desktop Provider on real apps (12/12 character mapping, select) |
| [sidepanel-manual-acceptance.md](evaluations/sidepanel-manual-acceptance.md) | Side Panel manual acceptance checklist |
| [ecosystem-interop.md](evaluations/ecosystem-interop.md) | Embedding the kernel into a random third-party OSS project (2026-09-27) |
| [competitor-openmuse-audit.md](evaluations/competitor-openmuse-audit.md) | Read-only source audit of OpenMuse-Win |
| [openssf-best-practices-self-assessment.md](evaluations/openssf-best-practices-self-assessment.md) | OpenSSF Best Practices Passing self-assessment (reviewed 2026-11-05) |

## 4. `docs/guides/` — host and operations guides (12)

| Document | Purpose |
|---|---|
| [production-permissions.md](guides/production-permissions.md) | Production permission setup (path referenced by kernel warnings; do not rename) |
| [demo-deployment.md](guides/demo-deployment.md) | Public deployment runbook (public instances require `YAI_API_KEY` since 2026-11-05) |
| [deployment-scaling.md](guides/deployment-scaling.md) / [en](guides/deployment-scaling.en.md) | Horizontal scaling and multi-tenant isolation |
| [tracing.md](guides/tracing.md) / [en](guides/tracing.en.md) | OpenTelemetry structured tracing |
| [multilang-interop.md](guides/multilang-interop.md) | Multi-language hosts (sidecar / MCP / OpenAPI) |
| [browser-extension-bridge.md](guides/browser-extension-bridge.md) | MV3 extension + Native Bridge: design, install, troubleshooting, acceptance |
| [execution-audit-persistence.md](guides/execution-audit-persistence.md) | Execution-audit persistence boundary |
| [federation.md](guides/federation.md) | Directed federation: parent calls child, never the reverse; protocol v1 / grants |
| [openapi-31-compat.md](guides/openapi-31-compat.md) | OpenAPI 3.1 / JSON Schema 2020-12 compatibility matrix |
| [ag-ui.md](guides/ag-ui.md) | AG-UI 1.0 adapter: event mapping, SSE framing, FastAPI example, boundaries |
| [mcp-registry.md](guides/mcp-registry.md) | Read-only MCP Registry discovery: search API, config builder, security boundary |
| [a2a-positioning.md](guides/a2a-positioning.md) | A2A 1.0 positioning: directed federation ≠ Multi-Agent; host-layer adapter |

## 5. `docs/reports/` — point-in-time reports (2)

| Document | Purpose |
|---|---|
| [audit-remediation-2026-11.md](reports/audit-remediation-2026-11.md) | v0.26 audit remediation plan and issue→fix mapping (5 batches) |
| [v026-交付报告.md](reports/v026-交付报告.md) | Branch delivery report before v0.26 (point-in-time; current state in IMPLEMENTATION_STATUS) |

## 6. `docs/adr/` — architecture decision records

Index: [adr/README.md](adr/README.md) (0002–0008, numbered from 0002).

## 7. `docs/walkthrough/` — line-by-line walkthrough (57, Chinese-only)

[00-index.md](walkthrough/00-index.md) is the overview and chapter index (01–56). The walkthrough is
intentionally Chinese-only because it is tied to Chinese comments. Build the Word handout with
`scripts/build_walkthrough_docx.py` (output goes to the git-ignored `docs/exports/`).

## 8. `docs/releases/` — GitHub Release source drafts (44)

Human-readable Release drafts for v0.6.0 and v0.10.0–v0.45.0 (v0.7–v0.9 predate standalone drafts).
Every draft now starts with a one-line **English summary**; the ledger in
[`CHANGELOG.md`](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CHANGELOG.md) remains authoritative.

## 9. Other READMEs

Shipped in sdist:
[benchmarks/agentdojo](https://github.com/Gi-Tuu/yai-agent-core/blob/main/benchmarks/agentdojo/README.md) ·
[benchmarks/memory](https://github.com/Gi-Tuu/yai-agent-core/blob/main/benchmarks/memory/README.md) ·
[examples/host_g_sandbox](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_g_sandbox/README.md) ·
[examples/child_cores](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/child_cores/README.md) ·
[examples/host_i_mealie](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_i_mealie/README.md) ·
[examples/host_j_actual](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_j_actual/README.md) ·
[examples/host_k_multimodal](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_k_multimodal/README.md) ·
[examples/host_l_sidecar_node](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_l_sidecar_node/README.md) ·
[examples/host_mcp_registry](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_mcp_registry/README.md) ·
[examples/host_a2a_adapter](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_a2a_adapter/README.md) ·
[examples/host_o_go_mcp](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_o_go_mcp/README.md) ·
[shell/desktop/fonts](https://github.com/Gi-Tuu/yai-agent-core/blob/main/shell/desktop/fonts/README.md)

Repository-only (not in sdist, names only): `packaging/extension/README.md`,
`packaging/windows/README.md`, `fuzzing/README.md`, `models/README.md`,
`clients/node/README.md`, `tests/fixtures/acc/SOURCE.md`, and the three `.github/` templates.

## 10. Resources and outputs

| Directory | Contents | In sdist |
|---|---|---|
| `docs/assets/` | Screenshots and demo assets used by README/docs | ✅ |
| `docs/figs/` | Whitepaper figures (`scripts/draw_figs.py`) | ✅ |
| `docs/exports/` | Word/PDF build outputs (`scripts/build_walkthrough_docx.py`) | ❌ git-ignored |

## 11. Local-only process material (not distributed)

Excluded by `.gitignore` / `.git/info/exclude`, visible only on the maintainer's machine:

- Work ledgers: `docs/项目台账.md` and its archives, `docs/v026-执行记录.md`
- Plans: `docs/v025-*`, `docs/v028-*`, `docs/YAI*.md`, `docs/产品化更新方案.md`,
  `docs/工具管理永久保留-落地方案.md`, `docs/检查点A-*.md`
- Internal: `docs/内部-*.md`, `docs/宣传视频-*.md`, `docs/终端工艺视觉-*.md`
- Tooling: `.zcode/`, root `AGENTS.md`, `YAI开源增长方案-修订版.md`
