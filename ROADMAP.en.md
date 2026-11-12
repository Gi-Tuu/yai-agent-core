> English translation of [中文](ROADMAP.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# Roadmap (English summary)

**English** · [简体中文](ROADMAP.md)

> This is a **concise English summary** of the Chinese roadmap. It keeps the direction, boundaries
> and version highlights; full per-version details live in [`CHANGELOG.md`](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CHANGELOG.md),
> GitHub Releases and [`docs/releases/`](https://github.com/Gi-Tuu/yai-agent-core/tree/main/docs/releases).

## Vision

Make "add agent capability to existing software" as simple as embedding a database: the host only
declares its business capabilities (ordinary functions + a one-line description); the YAI kernel runs
in-process and handles capability discovery, intent understanding, strategy selection, tool calls,
memory and auditionable delivery — **the host writes no Agent Loop / Planner / tool-selection code**.

## Design principles (unchanged across versions)

1. **Library-first** — YAI is imported, not deployed as a platform; the kernel keeps
   `dependencies = []` and every third-party capability is an optional, lazily-loaded extra.
2. **Everything external goes through SPI** — ten replaceable contracts: Model, Channel, Memory,
   Policy, Discovery, Sandbox, RouteSelector, Embedding, StorageCipher, ResourceProvider.
3. **One event stream** — execution collects events, `AgentLoop` yields them, and
   `AgentCore.astream` is the single emission point; every adaptive decision is visible.
4. **Fail closed** — permission, budget, timeout, lock and replay boundaries degrade safely and
   report diagnostics instead of silently continuing.
5. **Honest evidence** — `docs/IMPLEMENTATION_STATUS.md` separates *verified*, *implemented but not
   verified on real hardware*, and *not doing*; benchmarks and nightlies are linked to artifacts.

## Released versions (highlights)

| Range | Focus |
|---|---|
| v0.1 – v0.3 | Minimal kernel loop, tool sources, LLM routing, long-running reliability |
| v0.6 – v0.9 | Adaptive routing evolution, product shape, engineering gates, real-host end-to-end |
| v0.10 – v0.16 | Embedding safety, external-contract governance, silent-failure fixes, execution boundaries, language-agnostic sidecar |
| v0.17 – v0.25 | Host state anchors, desktop shell visual rework, Office/text extraction, on-the-spot verification, production readiness |
| v0.26 – v0.27.x | Execution capabilities & verifiable outcomes, real-hardware credibility, desktop/browser security tightening, docs site |
| v0.28 – v0.30 | Directed parent-child federation: in-process → local IPC → ecosystem ops (manifest hot-update, quotas, process supervision) |
| v0.31 | Long-term memory at 10k scale (bulk writes + incremental inverted index) |
| v0.32 | Interop: OpenAPI 3.1 / JSON Schema 2020-12, typed Node/TS sidecar client, GHCR image |
| v0.33 | Federation ecosystem tail: specialist bridge, usage/token budget, two installable child cores |
| v0.34 | Real-machine nightly (non-blocking): real Chromium + Windows UIA, honest artifacts |
| v0.35 | Desktop island CROSS-CORE visualization, lazy desktop package exports |
| v0.36 | Machine-readable `smoke_test.py --json` (community contribution) |
| **v0.37 (current)** | Bilingual documentation (30 pairs), good-first-issue claiming flow, offline OpenAPI 3.1 example (community contribution) |

## Next candidates (unordered, maintainer decides)

- **Cross-instance distributed rate limiting** — current global/tool limits are per-process; a
  multi-instance deployment needs shared state first.
- **Production sandbox ladder** — Docker is landed on the host side; next steps are gVisor, micro-VMs
  or remote execution, with zero kernel changes.
- **Visual orchestration for composite tools** — host-side UI; the kernel-side version has existed since v0.13.
- **Public-deployment exposure closure** — the Render demo was deleted on 2026-11-02; any future public
  instance must require `YAI_API_KEY` (or read-only demo mode) plus a global rate limit.
- **Multimodal follow-up** — promote the host-side resource resolver to more hosts; real token-cost
  accounting and backend capability probing landed in v0.13.
- **Real-machine acceptance** — close the remaining "manual only" items (real browser Side Panel, UAC,
  multi-monitor) with reproducible artifacts.

## v1.0 — feed back into AMBRACE

Ship the kernel embedded in [AMBRACE](https://github.com/Gi-Tuu/AMBRACE) (open-source AI companion),
with a stable SPI compatibility promise and semantic versioning.

## Explicitly not doing (to keep the kernel small)

- **No MCP Server** — YAI is a tool consumer, not an MCP provider.
- **No Multi-Agent orchestration** — that belongs to the host layer.
- **No built-in vector memory** — memory backends are SPI; vectors are host/plugin choices.
- **No built-in UI** — web/desktop/Flutter surfaces are host responsibilities; the event stream is enough.
- **No coding agent** — the kernel does not autonomously rewrite code.

## Maintenance cadence

- `main` stays green: full tests + ruff + mypy on Ubuntu 3.11/3.12/3.13 and Windows 3.13, plus
  CodeQL, Scorecard, fuzzing, routing benchmark and offline evaluation.
- Small PRs with Conventional Commits; behavior changes update tests and the walkthrough.
- Bilingual facade and international-facing docs are kept in pairs; see [`docs/BILINGUAL.md`](docs/BILINGUAL.md).
