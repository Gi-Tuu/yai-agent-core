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
- Summarization can use a dedicated cheap model (cost saving): with `AgentCore(compaction_model=small)`,
  the extra summarization call is routed to that model while the primary model focuses on the task; passing
  `None` (default) falls back to the primary model. The compaction model is an object argument, is not part
  of `CoreConfig`, and—like route classification—its calls are not counted against the task budget.
- Watermark convenience constructor: `CompactionPolicy.from_context_window(chars, *, hard_ratio=0.6,
  target_ratio=0.4)` derives the high/low watermarks as proportions of the per-session character workspace,
  and `from_context_window(40000)` equals the default 24000/16000. It stays character-based (no tokenizer);
  invalid ratios or an undersized window raise at construction time.
- Boundary: compaction only rearranges the current context and never rewrites persistent history in the
  MemoryStore; cross-request history trimming still goes through retention (§2.2).

### 2.6 Concurrency model and multi-tenant isolation

YAI is an in-process kernel running on a single-threaded asyncio event loop. Its concurrency contract is deliberately narrow and safe by default:

- **One `AgentCore` instance = one serial session.** At most one task runs at a time within an instance: `astream`/`run` hold an instance-level `asyncio.Lock` for their whole duration. Concurrent calls queue up instead of letting two tasks interleave at `await` points and corrupt the shared context, memory, tool registry, or routing-learning state.
- **For concurrency, use multiple instances.** Independent sessions (multiple tenants, specialists, or sub-employees) each construct their own `AgentCore`. Stateless model clients, discovery sources, and sandboxes may be shared singletons across cores, while stateful parts (`registry / memory / learning / code_tool_manager`) stay per-instance. Sub-employees (`shell/employee`) and each Dynamic Island specialist hold their own core this way and can still run in parallel (bounded by their own concurrency limits).
- **Closing a stream early releases the lock.** When the async generator is `aclose()`d (or the host breaks/discards it), the `async with` block releases the instance lock automatically. Hosts should fully drain or explicitly close the event stream; otherwise the next task waits.
- **Maintenance methods are synchronous.** `sweep_code_tools()` and memory retention run atomically on the event-loop thread (no `await` suspension), use snapshot iteration and atomic replacement for writes, and are meant to be called between tasks; they do not and should not share the task lock.

This reduces horizontal scaling to a simple rule: **many sessions on one host = many instances in one process; across machines = multiple replicas sharing stateless model/discovery backends.** The kernel itself introduces no thread pools, queues, or distributed locks.

### 2.7 Local diagnostic events (zero-dependency side channel)

At moments when the kernel **silently falls back so the main flow keeps running, but operations/debugging still needs visibility**, it emits a `DiagnosticEvent` through a callback that is **synchronous, dependency-free, and never able to break the main flow**. It complements, rather than replaces, the other three observability channels:

| Channel | Audience | Answers | Emitted by |
|---|---|---|---|
| `AgentEvent` (event stream) | Product / end user | What the agent is **deciding and doing** (routing, tool calls, discovery, authorization…) | Main kernel flow; always present, replayable |
| `DiagnosticEvent` (this section) | Local ops / debugging | Where the kernel **quietly degraded an enhancement** (model timeout, classifier fallback, discovery failure, compaction fallback…) | Only when the host passes an `on_diagnostic` callback |
| Metrics (dependency-free recorder, see below) | Ops / cost dashboards | How things run **in aggregate over time** (run outcome counts, tool error rate, strong-tier model calls, latency distribution) | A side-channel tap at the single `astream` exit; on by default |
| OpenTelemetry tracing (optional extra) | Distributed / cross-service | The **trace, latency, and attribution** of a request across processes | The host, subscribing to the event stream from the outside; the kernel initializes no exporter |

- **Shape:** `DiagnosticEvent(kind, message, level, data)`, with `level ∈ debug/info/warning/error`. The current eight `kind`s are `model_error`, `model_timeout`, `model_fallback`, `router_llm_fallback`, `discovery_failed`, `discoverable_list_failed`, `compaction_fallback`, and `schema_focus_fallback`.
- **Safety contract:** the callback is synchronous and should be fast and non-blocking (a log line or a metrics counter is enough). Any exception raised inside the callback is swallowed by the kernel: diagnostics are a side channel and must never take the agent down because "the logger crashed." The kernel does not depend on `logging` and turns no logging library into a hard dependency.
- **Wiring:** pass it once via `AgentCore(model, on_diagnostic=fn)`. The kernel duck-types the same callback onto the internal `AdaptiveRouter` and `FallbackModelProvider` (both expose a writable `on_diagnostic` attribute; custom host-built components that lack it are unaffected). With no callback the kernel stays completely silent and behaves exactly as before.
- **Deliberately excluded:** a sandbox failure surfaces as `tool_result(ok=False)` and a permission denial emits `permission_asked`; these are already user-visible business events, not silent fallbacks, so they are not re-emitted as diagnostics.

**Metrics (runtime metrics, dependency-free):** the `MetricsRecorder` in `metrics.py` taps the single `astream` exit — it marks run start, accumulates per event, and settles the outcome in `finally`. It counts runs (started/completed/failed/cancelled, with cancellation kept distinct from real errors), runs by strategy, tool total/errors, model tiers standard/strong (settled once on DONE), events, and a run wall-clock latency histogram (timed with `monotonic`). It uses only the standard library (one `threading.Lock` so cross-thread scraping is safe; `snapshot()` returns a deep copy) and `render_prometheus()` emits Prometheus text directly. The FastAPI battery exposes it at `GET /metrics` (text by default, `?format=json` for a snapshot); when an API key is configured it is authenticated by default, and `YAI_METRICS_PUBLIC=1` can expose it explicitly. A separate `GET /readyz` readiness probe runs concurrently with a 5s timeout and per-probe isolation — a failure removes traffic instead of restarting the process. For an OTel metrics pipeline use the optional `[otel]` extra `integrations/metrics.register_otel_meter`: all instruments are observable and pull from the snapshot only at scrape time (zero callbacks on the run hot path), and the library initializes no MeterProvider. As with diagnostics, any exception in a metrics callback is swallowed and can never break the main flow. See walkthrough chapter 38 and `docs/deployment-scaling.md` §7.

### 2.8 Production hardening P1: budget circuit breaker, at-rest encryption, centralized config, online backup

On top of P0 (concurrency contract / hard timeout / diagnostics), P1 addresses four **cost and data-safety** risks. All four are off by default or behave exactly as before, keep zero hard third-party dependencies in the kernel, and are fully tested offline.

- **Task-level hard budgets with a circuit breaker (prevent runaway spend):** `AgentCore(budget=Budget(max_model_calls=, max_tool_calls=, max_tokens=))`; `None` means unlimited and `0` means never allowed. The loop creates one `BudgetTracker` for the top-level task (clarification recursion reuses the same ledger). Model calls are counted at the single `_model_chat` entry point (**excluding** route classification and context compaction); normal tools are counted in the execution branch (meta-tools and composer inner steps are not double-counted; a composite tool counts as one call externally); tokens accumulate only when the model returns real `usage` (never estimated when missing). Hitting any cap terminates immediately, emits a `budget_exceeded` event (scope/used/limit plus a ledger snapshot), and the exception is explicitly re-raised in both tolerant fallback paths so it is never swallowed as a "degradation."
- **At-rest encryption for long-term memory (9th SPI, opt-in):** `spi/cipher.py` defines the synchronous `StorageCipher` (`encrypt/decrypt(bytes)`). `MemoryVault(path, cipher=...)` transparently encrypts on write and decrypts on read using atomic `os.replace`; a missing file, corrupt JSON, or **failed decryption (wrong key / tampered ciphertext) always cold-starts to an empty store and rewrites it**, so the process can always boot. With no cipher the vault stays plaintext JSON (backward compatible, documented as such). The algorithm stays out of the kernel: the `[crypto]` extra ships `FernetStorageCipher` (authenticated encryption) with `generate_key()` and `from_env("YAI_VAULT_KEY")` (a missing env var raises rather than silently minting a one-time key).
- **Centralized `CoreConfig` (prevent misconfiguration, enable reuse):** the zero-dependency `config.py` dataclass gathers every model-independent `AgentCore` option with defaults identical to the kwargs; `validate()` rejects bad values at construction time (non-positive counts, `bool` masquerading as an int, wrong types for `llm_router` / `context_compaction` / `budget` / `model_timeout`). `AgentCore.from_config(model, config)` lets multiple specialists / sub-employees share one budget-and-switch profile. The public kwargs signature and default behavior are unchanged.
- **Online SQLite hot backup and restore:** `SqliteStore.backup_to(dest)` takes a page-consistent snapshot inside the instance lock using the stdlib `Connection.backup`, with no downtime, for both file and `:memory:` stores; `restore_to(backup, dest)` restores offline and raises `sqlite3.DatabaseError` on a corrupt backup instead of silently opening an empty database. Crash recovery is guaranteed by WAL plus atomic writes (reopening the same path after close loses no data).

### 2.9 Semantic memory recall and proactive recall at task start (opt-in)

On top of the structured long-term memory (`MemoryVault`, §2.2), two enhancements ship **disabled by default and with zero hard kernel dependencies**:

- **Lexical OR semantic dual-channel recall:** `MemoryVault.arecall(query, *, k, kinds, tags)` is the async entry point. The lexical channel keeps token relevance; when the host injects the 8th SPI `EmbeddingProvider` via `AgentCore(memory_embedder=...)`, the semantic channel encodes the query and memory contents and scores cosine similarity (reusing the stdlib implementation in `discovery/semantic.py`, threshold 0.45). The two channels form a **union** keyed by memory id, the fused score is `max(lexical, semantic)` followed by the importance boost, so cross-language / paraphrased queries (a Chinese memory vs. an English query) are still recalled. The synchronous `recall` stays purely lexical with unchanged behavior; the `recall` tool handler becomes async and routes through `arecall`. Memory vectors are lazily batch-computed and cached by id; a `remember` merge or `forget` invalidates stale vectors; if the embedder raises or returns a mismatched count it **falls back to lexical recall** and records `semantic_last_error` (`status()` exposes `semantic_enabled` / `semantic_last_error`), so recall never fails because the embedding service is down.
- **Proactive recall at task start:** with `AgentCore(auto_memory_recall=True, auto_recall_k=3)`, before the user task the loop automatically recalls with the task text, injects hits as a single `system` message (worded so that an explicit instruction in the current turn always overrides stale memories), and emits a `memories_recalled` event (count plus each item's id/kind/summary). The clarify path (under-specified task) does not recall; any error in the recall callback only emits a local diagnostic and never blocks the task. The loop depends only on an `async (task) → list[dict]` callback (Core injects `functools.partial(vault.arecall, k=...)`) and never imports the vault or embedder. `CoreConfig.validate()` rejects at construction time: proactive recall without long-term memory, a non-positive `k`, and a non-async `embed_texts` embedder.

### 2.10 Bounded tool results and deterministic tool-catalog ordering (lightweight context governance)

Alongside the turn-level compaction in §2.5, two further pieces of "bytes fed to the model" governance are added; both have zero hard kernel dependencies, are fully offline-tested, and leave small results unchanged by default.

- **Uniform per-result bounding (C4):** for all three success paths — native functions, code tools (sandbox output), and composite tools (`{"steps":[...]}`) — `ToolExecutor` runs the `json.dumps` result through the pure helper `bound_tool_result(text, limit)` before writing it back into context. The default cap is **16,000 characters** (aligned with the OpenAPI integration's `max_result_chars`); on overflow it keeps the **first 70% and last 30%** with an explicit ellipsis marker in between stating how many characters were omitted, and a tightening loop guarantees the result never exceeds the cap — truncation is never silent. `AgentCore(tool_result_char_limit=...)` overrides it and `None` disables it; `CoreConfig.validate()` rejects non-positive integers and bools at assembly time. Successful `TOOL_RESULT` events gain `truncated` and `result_chars` (the original length before bounding), while the event `preview` stays at 200 characters; results within the cap are returned byte-for-byte unchanged. This is the closest-to-source bound on the "tool result → context" path; together with §2.5 (whole-turn 24,000-character compaction) and the HTTP-egress `payload_guard` (256 KB response cap) it forms three non-substitutable limits.
- **Deterministic tool-catalog ordering (C1):** the model-facing `catalog()` / `catalog_text()` / `describe()` / `llm_schemas()` of `ToolRegistry` all go through an internal `_ordered()` that sorts by tool name; the internal storage view `all()` still preserves registration order, and `schemas_for(names)` still follows the caller-supplied order. Consequently the tool-list bytes in the system prompt change only when the tool set **actually changes** (add/remove/rename), preventing incidental host registration-order churn from breaking the stable prompt-cache prefix and making snapshots deterministic.

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
| EmbeddingProvider | `embed_texts(list[str]) → list[vector]` | none (optional; local bge-m3 or OpenAI-compatible cloud) | Semantic tool discovery; semantic long-term memory recall (`memory_embedder`) |
| StorageCipher | `encrypt / decrypt(bytes) → bytes` | none (optional; `FernetStorageCipher` in the `[crypto]` extra) | At-rest encryption for the long-term memory vault (KMS / OS keychain / national ciphers) |

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

- Vendor-provided agent SDKs and graph orchestration frameworks: developer-builds-agent; tools/orchestration must be explicitly
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
- **No bundled vector database / vector index**: kernel memory is not bound to any vector store or ANN
  index. Semantic long-term memory recall (§2.9) reuses the optional `EmbeddingProvider` (local bge-m3 via
  the `[local-embed]` extra, or an OpenAI-compatible cloud) to encode memory contents on demand with lazy
  caching and compare cosine similarity; it targets a single host's structured memory (a small top-k by
  default) and introduces no resident vector database. Hosts that need large-scale vector retrieval can
  implement their own `MemoryStore` / recall callback.
- **Built-in UI**: the kernel provides no interface; the Dynamic Island / floating window lives in the
  shell layer (`shell/desktop/`) and talks to the kernel through the single embedding point.
- **Coding-agent scenarios, queues/middleware, K8s**: no unattended coding agent, no message middleware,
  and deployment does not depend on K8s (horizontal scaling is covered in
  `docs/deployment-scaling.md`).
