# Changelog (English)

[简体中文](CHANGELOG.md) · **English translation** of the version ledger; the Chinese [`CHANGELOG.md`](CHANGELOG.md) is canonical.

This file is a **version-level English summary** for international readers. Line-by-line changes, rationale and dates in each release remain in the Chinese ledger; per-release source drafts live in [`docs/releases/`](docs/releases/).

## Versions

| Version | Date | English summary |
|---|---|---|
| [v0.45.0](docs/releases/v0.45.0.md) | 2026-11-14 | LoCoMo scoring upgrades: temporal date normalization and an LLM judge (answerable F1 0.387, judge 0.424). |
| v0.45.1 | 2026-11-14 | English changelog, one-line English summaries for every historical release note, and an English document map. |
| [v0.46.0](docs/releases/v0.46.0.md) | 2026-11-14 | Read-only MCP Registry discovery (search + explicit config, no auto-install) and an A2A 1.0 positioning document. |
| [v0.47.0](docs/releases/v0.47.0.md) | 2026-11-14 | A host-layer A2A adapter example (remote agent → ToolSpec) and an explicit Registry connect/call example. |
| [v0.48.0](docs/releases/v0.48.0.md) | 2026-11-14 | A2A SSE streaming (`stream()` / `on_text`) and a Registry trust policy (domain allowlist, secret defaults rejected, verifier hook). |
| [v0.49.0](docs/releases/v0.49.0.md) | 2026-11-14 | A2A OAuth2 + push configs, Registry digest pinning, a Go MCP example, an offscreen QML test, and real-machine CONTRIBUTING docs. |
| [v0.50.0](docs/releases/v0.50.0.md) | 2026-11-14 | A2A authorization-code / device-code OAuth flows and an optional `go build` CI for the Go example. |
| [v0.44.0](docs/releases/v0.44.0.md) | 2026-11-14 | LoCoMo end-to-end memory QA: BGE-M3 top-5 recall plus a real model answering only from excerpts (answerable F1 0.348). |
| [v0.43.0](docs/releases/v0.43.0.md) | 2026-11-14 | LoCoMo lexical vs BGE-M3 retrieval comparison (R@5 0.348 → 0.507; hybrid MRR 0.436). |
| [v0.42.3](docs/releases/v0.42.3.md) | 2026-11-14 | A 27-second / 2.2 MB demo GIF covering CopilotKit chat, AG-UI events and the Box cloud desktop. |
| [v0.42.2](docs/releases/v0.42.2.md) | 2026-11-14 | CopilotKit demo hardening: a real tool call plus an npm build CI job. |
| [v0.42.1](docs/releases/v0.42.1.md) | 2026-11-14 | Box computer-use acceptance A–D: read screen, GUI typing, browser form write, cleanup. |
| [v0.42.0](docs/releases/v0.42.0.md) | 2026-11-14 | Box by Simular real-machine integration: 36 MCP tools (shell, browser, computer) over a personal key. |
| [v0.41.0](docs/releases/v0.41.0.md) | 2026-11-14 | A real CopilotKit v2 frontend demo (CopilotKit Runtime → HttpAgent → YAI /agent). |
| [v0.40.0](docs/releases/v0.40.0.md) | 2026-11-13 | A LoCoMo-style offline memory benchmark (10 conversations, 1,973 QA; lexical baseline). |
| [v0.39.0](docs/releases/v0.39.0.md) | 2026-11-13 | AG-UI 1.0 adapter: the single event stream translated into SSE frontend events, plus a FastAPI example. |
| [v0.38.0](docs/releases/v0.38.0.md) | 2026-11-13 | MCP 2026-07-28 stateless support, URL auth (headers/token_env), and tool-description poisoning defenses. |
| [v0.37.0](docs/releases/v0.37.0.md) | 2026-11-13 | A good-first-issue claiming flow, 30 bilingual documentation pairs, and an offline OpenAPI 3.1 example. |
| [v0.36.0](docs/releases/v0.36.0.md) | 2026-11-11 | Machine-readable smoke test output (`--json`, community contribution) and corrected status measurements. |
| [v0.35.0](docs/releases/v0.35.0.md) | 2026-11-10 | Cross-core calls visualized in the Dynamic Island panel; lazy desktop package exports. |
| [v0.34.0](docs/releases/v0.34.0.md) | 2026-11-10 | A non-blocking real-machine nightly workflow for Chromium and Windows UIA. |
| [v0.33.0](docs/releases/v0.33.0.md) | 2026-11-10 | Federation specialist bridge, usage/token quotas, and two installable child cores. |
| [v0.32.0](docs/releases/v0.32.0.md) | 2026-11-10 | GHCR image, typed Node/TS sidecar client, and OpenAPI 3.1 / JSON Schema 2020-12 compatibility. |
| [v0.31.0](docs/releases/v0.31.0.md) | 2026-11-10 | MemoryVault scales to 10,000 notes with batch writes; documents static-scan boundaries. |
| [v0.30.0](docs/releases/v0.30.0.md) | 2026-11-10 | Federation Phase 3 ops: manifest refresh, quotas, and per-edge statistics. |
| [v0.29.0](docs/releases/v0.29.0.md) | 2026-11-10 | Federation Phase 2: local IPC between processes with discovery, tokens and grants. |
| [v0.28.0](docs/releases/v0.28.0.md) | 2026-11-09 | Directed federation Phase 1: parent and child cores in one process. |
| [v0.27.3](docs/releases/v0.27.3.md) | 2026-11-09 | ADR 0008 directed-federation design and the GitHub Pages documentation site. |
| [v0.27.2](docs/releases/v0.27.2.md) | 2026-11-09 | Zero-key QuickStart, a reworked README first screen, and browser/desktop warning parity. |
| [v0.27.1](docs/releases/v0.27.1.md) | 2026-11-03 | Security patch: the desktop process allowlist moves from PID prefix matching to exact process identity. |
| [v0.27.0](docs/releases/v0.27.0.md) | 2026-11-03 | Controlled-browser real-task E2E, opt-in current-tab writes, and desktop select support. |
| [v0.26.0](docs/releases/v0.26.0.md) | 2026-11-03 | Browser Provider, MV3 extension + Native Messaging bridge, and the side panel LLM settings entry. |
| [v0.25.0](docs/releases/v0.25.0.md) | 2026-10-31 | Eight classes of audit hardening, an AgentDojo harness, and an egress-URL source gate. |
| [v0.24.0](docs/releases/v0.24.0.md) | 2026-10-27 | Prompt caching, tool-output guarding by default, and a receipt hash chain. |
| [v0.23.0](docs/releases/v0.23.0.md) | 2026-10-27 | One-command governance loop, one-command full gate, and a capability/evidence index. |
| [v0.22.1](docs/releases/v0.22.1.md) | 2026-10-25 | Release provenance assets, sidecar server end-to-end tests, and a Python 3.11 cancellation fix. |
| [v0.22.0](docs/releases/v0.22.0.md) | 2026-10-24 | Kernel-dependency guard tests, a real CODE_TOOL_RETIRED event, and runnable sdist tests. |
| [v0.21.0](docs/releases/v0.21.0.md) | 2026-10-24 | Office text extraction battery, QML nebula animation port, and test hygiene. |
| [v0.20.0](docs/releases/v0.20.0.md) | 2026-10-24 | Desktop shell visual redesign (Optical Obsidian / Braun Signal Orange). |
| [v0.19.0](docs/releases/v0.19.0.md) | 2026-10-24 | Sales CRM exposes its selected customer through HostStateProvider. |
| [v0.18.0](docs/releases/v0.18.0.md) | 2026-10-24 | Desktop companion uses HostStateProvider for live context. |
| [v0.17.0](docs/releases/v0.17.0.md) | 2026-10-24 | Node sidecar example plus HostStateProvider integration in real hosts. |
| [v0.16.0](docs/releases/v0.16.0.md) | 2026-10-24 | Language-agnostic sidecar session protocol (stdio JSON-RPC) for non-Python hosts. |
| [v0.15.0](docs/releases/v0.15.0.md) | 2026-10-24 | Held-out router evaluation, Windows junction race tests, and live host-state providers. |
| [v0.14.0](docs/releases/v0.14.0.md) | 2026-10-23 | Multimodal core checks, opt-in strict_direct mode, and capability health reporting. |
| [v0.13.0](docs/releases/v0.13.0.md) | 2026-10-22 | Argument validation expands from 4 to 13 schema keywords; multimodal cost accounting and workflow versioning. |
| [v0.12.0](docs/releases/v0.12.0.md) | 2026-10-22 | A multimodal message contract (text/image/resource parts) with explicit limits and no silent image drops. |
| [v0.11.0](docs/releases/v0.11.0.md) | 2026-10-16 | Fixes PEP 563 string-annotation schemas and approval-threshold export; schema inspection becomes reliable. |
| [v0.10.0](docs/releases/v0.10.0.md) | 2026-10-14 | Capability declarations travel across processes; tool timeouts, idempotent close, and an Anthropic backend. |
| v0.9.0 | 2026-10-12 | Third-party seeding E2E on Actual Budget and the five-beats standard line. |
| v0.8.1 | 2026-10-11 | OpenSSF Best Practices Passing badge, English front-door docs, and signed releases with SLSA provenance. |
| v0.8.0 | 2026-10-10 | Code-tool management, the three-state Dynamic Island, and a chat-first panel. |
| v0.7.0 | 2026-09-22 | Bundled ScriptedModel, FallbackModelProvider, and the finished host-E CRM workbench. |

Older historical release notes (v0.10.0–v0.36.0) carry an English-summary line at the top
of each source draft; v0.7.0–v0.9.0 predate standalone release drafts.
