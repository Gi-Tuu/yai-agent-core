# YAI Agent Core — Architecture

[English](architecture.en.md) · [简体中文](architecture.md)

## 1. Design principles

1. **Library-first (in-process library)**: after `pip install` it runs inside the host process; it does
   not start a separate service and does not require the cloud.
2. **Host-declares-capability**: the host only declares capabilities (ordinary functions / OpenAPI /
   MCP); Core owns all agent logic.
3. **Minimal kernel**: the test is "does *every* host need this?" — otherwise it goes into `batteries/`
   or an adapter.
4. **Zero hard dependencies**: the kernel uses only the standard library; openai / fastapi are optional,
   lazily-loaded dependencies.
5. **Adaptivity must be explainable**: every decision produces an Observer event; no black boxes.
6. **Model/channel/storage-agnostic**: decoupled through SPI contracts; Core provides zero-config
   defaults.

## 2. Layering

```
Host application (AMBRACE / host_a / host_c)      declares capabilities only, zero agent code
        │ introspection / registration
Capability auto-discovery (functions / OpenAPI[v0.3] / MCP Client[v0.2, shipped])
        ▼
YAI Kernel
  ├─ Adaptive Router   classify task → direct/react/plan/clarify + model tier
  │                     tools insufficient → capability_missing → ToolDiscovery adds tools (usable this turn)
  ├─ Agent Loop        execute per strategy (tool loop, plan decomposition, clarify re-entry, gap discovery)
  ├─ Context           message assembly and token budget
  └─ Tool Bus          permission → execution (sync/async) → structured result
        ▲
SPI contract ring (replaceable + default implementations)
  ModelProvider (OpenAI-compatible) / Channel (CLI · FastAPI · Flutter)
  MemoryStore (in-memory → SQLite → vector) / PermissionPolicy (auto/ask/deny)
  ToolDiscovery (StaticCatalog in-process catalog → MCP/OpenAPI/plugin marketplace)
        ▲
Integrations (optional, lazy): MCP Client (integrations/mcp/, [mcp] extra), OpenTelemetry tracing (integrations/tracing/, [otel] extra, subscribes to the event stream from outside without touching the kernel)
Batteries (optional): FastAPI Server, SQLite Memory
```

### 2.1 MCP Client integration (shipped in v0.2)

```
MCP Server (HTTP / stdio subprocess / in-memory instance)
   │  list_tools / call_tool (MCP Python SDK v2, optional dependency, lazily loaded)
   ▼
McpToolBridge (integrations/mcp/client.py)
   ├─ sanitize_schema      strip private keys from SDK-generated schemas
   ├─ _to_spec             each remote tool → ToolSpec(source="mcp"), handler is an async closure
   └─ _flatten_call_result content text / structured_content / is_error → return value or exception
   ▼
ToolRegistry (isomorphic to native tools; Router/Loop/Executor are unaware of the difference)
```
- The kernel itself never imports `mcp` (an AST test guards the red line); enable with
  `uv sync --extra mcp`.
- Three connection modes: `McpServerConfig.url` (Streamable HTTP) / `command+args` (stdio subprocess) /
  `server` (in-memory instance, for offline tests); `prefix` resolves tool-name clashes across servers.
- Lifecycle: `async with McpToolBridge(...)` or manage manually with `connect()/aclose()`; the stdio form
  must be `aclose()`d or the subprocess is orphaned.

### 2.2 Persistent memory (SQLite, shipped in v0.2)

`memory/sqlite_store.py` provides `SqliteStore`, a SQLite implementation of the MemoryStore contract
(standard library, no new dependencies). Two tables, `history` and `kv`; a `scope` column isolates
sessions; `PRAGMA user_version` is the migration hook; WAL plus a single lock covers single-process
concurrency; `history()` keeps the contract's synchronous signature. Wire it via `YAI_DB_PATH`
(`SqliteStore.from_env()`); when unset it falls back to InMemoryStore, and it is off by default online
(the free PaaS disk is ephemeral). Loop/Core/host examples need no changes. Boundaries (history trimming,
multi-process writes, ephemeral-disk persistence) are in walkthrough chapter 11, section H (zh-CN).

History retention (v0.3, opt-in): `memory/retention.py` provides pure functions `count_cut`/`ttl_cut`;
both stores support them symmetrically via constructor args `max_messages` / `ttl_seconds` / `clock`.
Trim boundaries align to "turns" (from one user message to just before the next), so an
assistant(tool_calls)/tool pair is never split; the SQLite store prunes once on construction to cover the
restart case. The MemoryStore contract and schema version (still v1) are unchanged; KV TTL needs a schema
v2 migration, deferred to v0.4. The `Context` character-budget hard truncation (formerly the private
`_compact`, now the public `enforce_budget`) drops at turn boundaries the same way (same-origin
implementation in the kernel); v0.7 adds summary compaction on top of it — see §2.5.

### 2.3 OpenAPI discovery (shipped in v0.2)

`integrations/openapi/` auto-translates any OpenAPI 3 description into `ToolSpec(source="openapi")` (the
alias is only for logs and notes). `spec.py` handles loading / version gating / inlining internal `$ref`
(cyclic and external refs are retained and noted); `discovery.py` is a pure function that plans
operations into tools (operationId/synthetic naming, merging path/query/body inputs, include/exclude,
`read_only` keeping only GET/HEAD, truncating at `max_operations=40`); `client.py` executes with lazily
loaded httpx (bearer/apiKey tokens from environment variables, validated at call time, and normalizes
204/non-2xx/JSON/text truncation). httpx/pyyaml live in the `[openapi]` extra; the kernel itself has zero
hard dependencies; all tests run offline through `httpx.MockTransport`. Like the MCP bridge, it is mounted
in order in the serve_example app lifespan and shares the shutdown list.

### 2.4 On-demand capability discovery loop (ToolDiscovery, minimal slice shipped)

```
The LLM router decides existing tools are insufficient (missing_capability)
   │  emit capability_missing (with the gap and the available-tool list)
   ▼
ToolDiscovery.discover(need, task, available) → list[ToolSpec]
   │  built-in StaticCatalog: in-process candidates + explicit keyword matching (zero-dep, offline-testable)
   │  future: MCP directory / OpenAPI service directory / plugin marketplace
   ▼
The kernel dedups uniformly (same name across candidates + already registered) → ToolRegistry.register_many
   │  emit tool_discovered (missing / source / registered)
   ▼
This turn's ReAct: the new tools are already in the system prompt and tool list, callable this turn
   └─ execution still goes through PermissionPolicy (discovery ≠ authorization; a discovery-source error only emits an error event, never fatal)
```

- Discovery sources only hand over candidate specs; **registration/dedup/permissions stay in the kernel**,
  so external sources cannot bypass auditing.
- Discovery happens before the system prompt is built, so new tools are visible to the model in the
  **same turn**, without waiting for the next task.
- Safe default: with `discovery=None` it only emits the gap event and never registers tools dynamically,
  so legacy host behavior is unchanged.
- Boundary: currently discovery runs once when "the router judges a gap"; mid-execution reflection,
  semantic retrieval and dynamic source switching landed in v0.7/v0.8 (see ROADMAP).

### 2.5 Context summary compaction (shipped in v0.7, opt-in)

```
Before each ReAct model call: is Context.total_chars() above the hard_max_chars high-water mark?
   │  no → call the model as usual
   ▼ yes
select_compaction_range: start = first message of the oldest old user turn,
                         end = first message of the keep_recent_rounds-th-from-last user turn
   │  render_for_summary: old turns to plain text (user / assistant[tool calls] / tool results)
   ▼
Summary model achat([summary instruction, old-turn text], tools=None)
   │  success → replace_range swaps the span for one system "summary of earlier conversation", emit context_compacted
   └─ error / empty summary → fall back to enforce_budget() hard truncation (turn boundaries, never split tool pairs), never fatal
   ▼
The most recent keep_recent_rounds complete turns stay intact, so long conversations keep early goals and facts
```

- Three-part window: system (index 0) is always kept / old turns compress into one summary / recent
  complete turns are fully retained; `replace_range` enforces `start≥1` and aligns both ends to user-turn
  starts, so the message structure is always valid for the model.
- The summary is an enhancement, not a dependency: model errors, timeouts and empty summaries all fall
  back to the proven hard truncation `enforce_budget()`.
- Switch: `AgentCore(context_compaction=True)` uses the default `CompactionPolicy` (hard 24000 / target
  16000 / keep 2 turns), or pass a custom policy; off by default so legacy host behavior is unchanged. The
  Dynamic Island live factory enables it.
- Boundary: compaction only rearranges the current context and never rewrites persistent history in the
  MemoryStore; cross-request history trimming still goes through retention (§2.2).

### 2.6 Concurrency model and multi-tenant isolation

YAI is an in-process kernel running on a single-threaded asyncio event loop. Its concurrency contract is deliberately narrow and safe by default:

- **One `AgentCore` instance = one serial session.** At most one task runs at a time within an instance: `astream`/`run` hold an instance-level `asyncio.Lock` for their whole duration. Concurrent calls queue up instead of letting two tasks interleave at `await` points and corrupt the shared context, memory, tool registry, or routing-learning state.
- **For concurrency, use multiple instances.** Independent sessions (multiple tenants, specialists, or sub-employees) each construct their own `AgentCore`. Stateless model clients, discovery sources, and sandboxes may be shared singletons across cores, while stateful parts (`registry / memory / learning / code_tool_manager`) stay per-instance. Sub-employees (`shell/employee`) and each Dynamic Island specialist hold their own core this way and can still run in parallel (bounded by their own concurrency limits).
- **Closing a stream early releases the lock.** When the async generator is `aclose()`d (or the host breaks/discards it), the `async with` block releases the instance lock automatically. Hosts should fully drain or explicitly close the event stream; otherwise the next task waits.
- **Maintenance methods are synchronous.** `sweep_code_tools()` and memory retention run atomically on the event-loop thread (no `await` suspension), use snapshot iteration and atomic replacement for writes, and are meant to be called between tasks; they do not and should not share the task lock.

This reduces horizontal scaling to a simple rule: **many sessions on one host = many instances in one process; across machines = multiple replicas sharing stateless model/discovery backends.** The kernel itself introduces no thread pools, queues, or distributed locks.

## 3. SPI contracts

| Contract | Methods | Default implementation | When a host replaces it |
|---|---|---|---|
| ModelProvider | `achat(messages, tools, *, tier)` | OpenAICompatProvider (DeepSeek, etc.) | Private/local models |
| Channel | `emit / ask / confirm` | CollectChannel, CliChannel | Web SSE, Flutter UI |
| MemoryStore | `history / put / get / clear` | InMemoryStore | SQLite, vector memory |
| PermissionPolicy | `check(tool, args) → allow/deny/ask` | AllowlistPolicy | Human confirmation for risky tools |
| ToolDiscovery | `discover(need, *, task, available) → list[ToolSpec]` | StaticCatalog (in-process catalog) | Dynamic discovery from MCP/OpenAPI/marketplaces |
| ToolSandbox | `execute(code, inputs, *, timeout=None) → SandboxResult` | none in the kernel (host-provided; `SubprocessSandbox`/`DockerSandbox` in the shell/examples) | Running generated code in isolation |
| RouteSelector | route selection + observe feedback (contextual bandit) | the built-in self-calibrating bandit | A custom routing/learning policy |
| EmbeddingProvider | `embed_texts(list[str]) → list[vector]` | none (optional; local bge-m3 or OpenAI-compatible cloud) | Semantic tool discovery with your own embedder |

## 4. Adaptive Router state machine

```
Task text + available tool set
   ├─ no tools                → direct (the model answers directly)
   ├─ vague short utterance   → clarify (Channel.ask then re-enter)
   ├─ action verbs + multi-step signals → plan (decompose first, then react)
   └─ action verbs            → react (tool loop)
```

v0.2 (shipped): the LLM classifies once, emitting `{strategy, reason, tier, missing_capability}`; on
error/timeout/invalid output it falls back to rule routing. Both paths return a unified RouteDecision, and
the `strategy_selected` event carries `source=llm|rules` for auditability. LLM routing also senses the
capability boundary: when existing tools cannot finish the task, after `strategy_selected` it emits a
`capability_missing` event (with the gap description and the available-tool list); if a `ToolDiscovery`
source is injected, the kernel then closes the gap into new tools (§2.4), otherwise it only emits the
event and lets the host guide the user. The rule path, clarify, and no-tool deployments do not emit this
event. With `llm_router="auto"`, LLM classification is enabled according to the model backend's
`yai_live_router` capability flag (real backends declare it; offline scripted models do not).

## 5. Starting the online API (a battery)

```python
import uvicorn
from yai_core import AgentCore, OpenAICompatProvider
from yai_core.batteries.fastapi_server import create_app
import my_app_capabilities as cap

core = AgentCore.auto(cap, OpenAICompatProvider())
app = create_app(core)
# Env vars: YAI_GIT_COMMIT=<40-char commit>, YAI_PROJECT_SLUG=<slug>
# The container listens on 8000; the host maps the port (this repo's convention is 8001:8000)
# uvicorn module:app --host 0.0.0.0 --port 8000
```

Review-period rate limiting (on by default): `rate_limit.py` provides a per-IP sliding window
(`enabled=True`, `per_minute=30`, `window_seconds=60`; tune or disable with `YAI_RATE_LIMIT_*`). The
middleware only intercepts `POST /v1/agent/run`, returning 429 + `Retry-After` on overflow (same origin
as the response body); limited requests never reach the model. The three GET verification endpoints are
never limited (a review hard gate). Client identity is the leftmost non-empty XFF value, falling back to
the peer address; state is in-memory only and clears on restart; there is no global fallback limiter
(single instance).

## 6. Differentiation from the ecosystem (honest comparison)

- Claude/OpenAI Agents SDK, LangGraph: developer-builds-agent; tools/orchestration must be explicitly
  defined and they bind to their own ecosystems;
- agentic-kernel (TS): a similar embedded-runtime idea, but TypeScript-only and without capability
  auto-discovery or adaptive strategy;
- MCP: a tool-interoperability **protocol**, not a runtime; YAI is a consumer of MCP (v0.2 Client), not a
  competitor;
- YAI: host-declares-capability, in-process Python, fully model/channel/storage-agnostic, observable
  adaptive process.

## 7. Explicit non-goals (kernel boundary)

The following do **not** enter the kernel itself (`src/yai_core/`), to preserve zero hard dependencies and
embeddability; some are already provided by the shell layer or optional extras, noted in parentheses.

- **MCP Server**: the kernel is only an MCP Client (connecting to external servers); it does not expose
  itself as a server.
- **Peer-to-peer multi-agent orchestration**: no role-equal multi-agent negotiation. Hierarchical
  "sub-worker delegation" is shipped in the shell layer (`shell/employee/`: a single root is accountable
  to the user; sub-workers have monotonically narrowed permissions, a budget, and are destroyed when done).
- **Executing generated code inside the kernel**: composed tools and sandbox code tools are supported, but
  generated code always runs inside a host-provided `ToolSandbox` (the kernel ships no executor), governed
  by an approval gate and TTL lifecycle.
- **Vector long-term memory**: kernel memory is not bound to a vector store; local bge-m3 embeddings are
  only an optional extra (`[local-embed]`) for semantic tool discovery, not long-term memory.
- **Built-in UI**: the kernel provides no interface; the Dynamic Island / floating window lives in the
  shell layer (`shell/desktop/`) and talks to the kernel through the single embedding point.
- **Coding-agent scenarios, queues/middleware, K8s**: no unattended coding agent, no message middleware,
  and deployment does not depend on K8s (horizontal scaling is covered in
  `docs/deployment-scaling.md`).
