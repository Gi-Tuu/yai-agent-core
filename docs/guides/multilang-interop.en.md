> English translation of [中文](multilang-interop.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# Multilang Interop Design

**English** · [简体中文](multilang-interop.md)

> Fact baseline: yai-agent-core **v0.15.0**, last verified **2026-10-24**.

## 0. First answer: can it be done, or is it already done?

| Capability | Status | Notes |
|---|---|---|
| **Cross-language integration** (connecting services written in any language) | **✅ Implemented** | Via the **MCP Client** and **OpenAPI Discovery**; both are language-neutral, covered by offline tests and already in production use (online it is wired to the DeepWiki MCP) |
| Python **in-process zero-config introspection** | **✅ Implemented** | `discover(host)` uses `inspect` to generate ToolSpecs automatically, **Python only** |
| `ToolSpec` neutral data + `registry.register()` | **✅ Implemented** | A tool capability is data and is not bound to a language |
| **ToolSpec ↔ manifest JSON serialization + file IO** | **✅ Implemented** | `to_manifest_dict` / `from_manifest_dict` + `tools/manifest.py`; composite / code tools can be fully ported and executed, native ones only export declarations; governance declarations (redaction paths / broad audit / approval requirements / contract snapshot / timeout / rate limiting) travel with the manifest, while functions (`gate` / `preview` / `idempotency_key`) do not |
| **Sidecar session control (stdio JSON-RPC)** | **🛠 Landed on main, release pending** | `python -m yai_core.sidecar`: carries only initialize / run / cancel / approval / clarify / events; **the tool surface still goes through MCP / OpenAPI**, and no new tool-call channel is added |
| Non-Python **in-process zero-config introspection** | ❌ Parity not pursued | Static languages have no equivalent runtime metadata, so they need an "annotation / generation" step (a language property, not a defect) |
| Per-language annotations / code generators (JVM APT, Rust proc-macro, `go:generate`, TS extraction) | 🛠 Not done (roadmap) | See §4 |
| Per-language native exporter probes | 🛠 Not done (roadmap) | See §4 |
| gRPC Proto / GraphQL / LSP discovery adapters | 🛠 Not done (roadmap) | Currently only MCP / OpenAPI |
| Cross-language conformance test suite | 🛠 Not done (vision) | See §5 |
| Kernel multilingual rewrite / FFI bindings | 🚫 Not doing | Replaced by a conformance contract, see §5 |

> In one sentence: **"calling the tools of a service written in another language" works today (MCP/OpenAPI); "in-process zero-config introspection in another language" is not done and parity is not pursued — that route goes through annotations / probes / standard protocols.**

---

## 1. Core idea: the Core depends on a neutral manifest, not on some language's reflection

The essence of auto-discovery = **obtaining a machine-readable capability list (a manifest, i.e. `ToolSpec` / JSON Schema)**.
Python's `inspect` is merely **one of the providers** that "generate the manifest":

```
                  AgentCore (introspection → gap awareness → discovery / composition)
                                             │
                    uniform ToolSpec loading (language-neutral manifest)
   ┌────────────────────┬────────────────────┬────────────────────┬────────────────────┐
    Python               annotations /        standard             hand-written
    introspection        code generation      protocols            manifest
    inspect(✅)           JVM APT / proc       OpenAPI(✅)           JSON / YAML
                         Go go:generate       MCP(✅)               fallback
```

---

## 2. Provider matrix: language → manifest source

| Language / runtime | Most convenient manifest source | Same process? | Status |
|---|---|---|---|
| Python | `inspect` automatic introspection | In-process, zero config | ✅ |
| JVM (Java/Kotlin) | annotations + APT generation / reflection | an in-process setup needs a JVM kernel; the general route is MCP/OpenAPI | MCP/OpenAPI ✅; APT 🛠 |
| TS / JS / Node | TS compiler API / decorators | a sidecar MCP is the least effort | MCP/OpenAPI ✅; decorators 🛠 |
| Go | struct tags + `go:generate` | compile-time generation | MCP/OpenAPI ✅; generate 🛠 |
| Rust | proc-macro / build script | compile-time generation | MCP/OpenAPI ✅; macro 🛠 |
| C# | Source Generator / reflection | compile time / reflection | MCP/OpenAPI ✅ |
| **Any REST/RPC service** | **MCP / OpenAPI** (Proto/GraphQL later) | **out of process, naturally cross-language** | ✅ |

---

## 3. Cross-language wiring available today (✅ implemented)

### 3.1 Via MCP (have the other language start an MCP Server)

- Core acts as an **MCP Client** and supports two transports: an **stdio subprocess** and **Streamable HTTP**;
- It speaks the **MCP 2026-07-28 stateless spec** (SDK v2.2+; `protocol_mode="auto"` discovers first);
  URL targets support `headers` / `token_env` authentication, and remote tool descriptions pass through
  untrusted-text sanitization first;
- Tools exposed by an MCP Server written in any language are registered into the `ToolRegistry`;
- Configuration (see `.env.example`): `MCP_SERVER_URL=<http form>` or `MCP_SERVER_COMMAND=<stdio form>`.

### 3.2 Via OpenAPI (have the other language's HTTP service produce an OpenAPI description)

- Give it an OpenAPI spec (an online URL or a local file, JSON/YAML) and its endpoints are registered as tools automatically;
- Configuration: `OPENAPI_SPEC_URL` / `OPENAPI_SPEC_PATH`; for public demos, `OPENAPI_READ_ONLY=1` registers GET/HEAD only.

> Neither route **requires the other side to be Python**; they are the cross-language integration routes verified today.

### 3.3 Via sidecar session control (✅ implemented)

A non-Python host that wants to run the **whole Agent** (not just a few tools) can spawn a Python subprocess:

```bash
python -m yai_core.sidecar     # NDJSON + JSON-RPC 2.0 on stdin/stdout
```

- `initialize`: configure the model, MCP/OpenAPI, and the permission tier;
- `agent/run`: start a task and stream `agent/event`; the final answer arrives in the `done` event;
- `agent/cancel`, `shutdown`;
- approvals / clarifications are initiated back by the sidecar as `approval/request` / `clarify/request`, and the host UI answers.

**Tools still come in only through MCP / OpenAPI**: the host wraps its own business functions into an MCP Server (or REST service) in any language, and the session protocol has no `tool/call`. That way the host reuses the existing tool adapters without duplicating MCP. See `docs/walkthrough/54-sidecar-protocol.md`.

Runnable sample: `examples/host_l_sidecar_node/` (zero-dependency Node, a local mock model + Node's own OpenAPI tools, actually spawning the sidecar and going through the reverse approval request).

### 3.4 Typed Node/TS client (✅ since v0.32)

`clients/node/` provides a TypeScript client with zero runtime dependencies (Node ≥ 20) that wraps the session surface as a typed API: `initialize` / `run` / `cancel` / `ping` / `shutdown`, event subscription and `waitForEvent`, approval / clarification callbacks (**deny** by default, fail closed), and process exit / timeout mapped to `SidecarError`.

```ts
import { SidecarClient } from "yai-agent-core-sidecar-client";

const client = SidecarClient.spawn({ onApproval: () => true });
const init = await client.initialize({ model: { model: "deepseek-chat" } });
await client.run("check my order");
const done = await client.waitForEvent((event) => event.type === "done");
console.log(done.data.final_text);
```

Build and test: `cd clients/node && npm install && npm test` (offline, fake sidecar fixture);
CI: `.github/workflows/client-node.yml`.

---

## 4. Not done yet, and how it evolves (roadmap, ordered by value for effort)

1. **Per-language manifest generators**: a JVM annotation processor (APT), Rust proc-macro, Go `go:generate`, TS compiler extraction — producing `ToolSpec` JSON at compile time.
2. **Native exporter probes**: one very small program per language that reflects on the host side and emits manifest JSON over stdio/HTTP; Core understands only JSON and does not care about the language.
3. **More standard protocol adapters**: gRPC (Proto), GraphQL, LSP (JSON-RPC) — at the same layer as MCP/OpenAPI.

> The kernel's Python code does not need to change for any of this; all of it is a newly added **manifest provider**.

> **Boundary: do not build a separate "manifest + proprietary JSON-RPC tool-call channel"** — that would amount to a simplified MCP.
> The manifest is responsible only for "the language-neutral description and exchange of capabilities"; actual cross-language tool calls uniformly go through MCP / OpenAPI.
> The sidecar's JSON-RPC **carries only Agent session control** (run, cancel, approval, clarify, events),
> not tool calls, and therefore does not violate this boundary.

---

## 5. The conformance vision: multilingual "same origin" without rewriting the kernel

Rather than rewriting the kernel in Java/Rust and maintaining FFI bindings (not feasible for one person), publish three **language-neutral** things:

1. **manifest schema** (the JSON serialization of `ToolSpec`, **already available**: `to_manifest_dict` + `tools/manifest.py`);
2. **behavior contract** (the semantics of introspection → gap awareness → discovery / composition);
3. **cross-language conformance test suite**.

YAI implementations in each language can **grow independently and share no code**; they count as compatible as long as they pass the same conformance suite.
This lets "multilingual support" be driven by the community / demand, so the kernel author does not carry the maintenance cost of many languages.

---

## 6. Honest boundaries

- **In-process, zero-config introspection is a dividend of dynamic languages (Python)**; static languages inherently need an extra "annotation / generation" step.
- The goal is "**every language can integrate with the lowest friction that ecosystem allows**", not "all languages are as zero-config as Python".
- What is truly portable is **the adaptive mental model and the contracts**, not the Python code itself.

---

## Appendix: related files

| File | Purpose |
|---|---|
| `src/yai_core/integrations/mcp/client.py` | MCP Client (stdio + HTTP) |
| `src/yai_core/integrations/openapi/{discovery,spec,client}.py` | OpenAPI discovery (JSON/YAML) |
| `src/yai_core/tools/manifest.py` | manifest bulk import/export + file IO |
| `src/yai_core/sidecar/` | Sidecar session control protocol (stdio NDJSON JSON-RPC) |
| `clients/node/` | Typed Node/TS sidecar client (zero runtime dependencies + offline tests) |
| `src/yai_core/discovery/` | Python function introspection |
| `src/yai_core/spi/` | 10 capability contracts |
| `docs/walkthrough/10-mcp-client.md` / `12-openapi-discovery.md` | Line-by-line walkthrough |
