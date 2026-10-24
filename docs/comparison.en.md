# YAI Agent Core vs. Alternatives (an honest comparison)

**English** · [简体中文](comparison.md)

> Last updated: 2026-10-24. This document states only **verifiable facts**; YAI's side is based on this
> repository's code/config. To avoid restating third parties' own marketing copy, alternatives are
> described **by category** (general framework / graph runtime / multi-agent framework / self-hosted
> platform / hosted no-code) — no names, no slogans, no vendor metrics. Category traits reflect public
> common knowledge and may drift; treat each product's own documentation as authoritative. No disparaging
> comparisons; the end says which category to pick in which scenario.

## 1. One-line positioning (by category)

| Option | Positioning | Form |
|---|---|---|
| **YAI Agent Core (this project)** | An in-process, embeddable, self-adaptive Agent **kernel**: the host only declares capabilities; the kernel auto-discovers tools, routes adaptively and executes | A Python library (`pip install`); the kernel itself has zero third-party hard dependencies |
| General agent frameworks (category) | Provide tool abstractions, chain composition and middleware; the developer assembles an agent | Python/JS library or framework |
| Low-level graph orchestration runtimes (category) | Express an agent as nodes/edges and a state machine, emphasising precise control and long-running flows | Python/JS framework |
| Multi-agent frameworks (category) | Take "roles + collaboration/conversation" as the first abstraction, orchestrating several agents | Python framework |
| Self-hosted LLM app platforms (category) | A server-side platform with visual orchestration, plugin marketplace and ops panel; apps call into it | Self-hosted platform (server + web console) |
| Hosted no-code platforms (category) | Configure bots/workflows by dragging on a web page; execution and operation live in the platform | Hosted SaaS |

## 2. Comparison across key dimensions

| Dimension | YAI Agent Core | General / graph frameworks | Multi-agent frameworks | Self-hosted platforms | Hosted no-code platforms |
|---|---|---|---|---|---|
| Delivery form | An imported library running in the host process | Library/framework | Library/framework | Self-hosted server platform | Hosted SaaS |
| What the developer does | Write ordinary business functions and declare capabilities; **no Agent Loop/Planner/tool selection** | Define agents, tools, chains/graphs, states and edges yourself | Define roles, tasks, collaboration flows yourself | Configure prompts, tools, workflows in the platform | Drag-and-drop a Bot/plugin on a web page |
| Embedding into existing software | In-process `import`, one-line `AgentCore.auto`; no server introduced | Embeddable, but you must implement agent assembly and the loop yourself | Embeddable, but the multi-agent paradigm needs app cooperation | The app becomes a platform client/external caller | Cannot embed; the app runs inside the platform |
| Kernel runtime deps | **`dependencies = []`** (openai/fastapi/mcp/httpx are all optional, lazily-loaded extras) | A set of layered dependency packages | Framework dependencies | A full server stack (multi-container orchestration) | None (hosted) |
| Tool onboarding | Function introspection auto-registration; **MCP Client**; **OpenAPI auto-discovery**; **on-demand ToolDiscovery loop** | Tool abstractions + many integration packages | Tool abstractions | In-platform tools/plugin marketplace | Platform plugin ecosystem |
| Tool discovery/matching | **Dual channel: lexical (zero-dep, Chinese bigrams, synonyms) + optional embedding semantics**, stdlib cosine, two-layer summaries, margin to prevent false recall; degrades to pure lexical with no embedder configured | Recall is chosen by the host (built-in depth varies by product) | Usually delegated to host-side components | In-platform tool/plugin set, chosen by configuration | Platform plugin marketplace |
| Orchestration paradigm | Adaptive routing (direct/react/plan/clarify) + a minimal capability-gap loop (sense→discover→register→usable this turn); the host presets no flow | Chains/middleware; graphs and state machines | Role teams / conversation-driven | Visual workflow graph | Visual orchestration |
| Routing self-calibration | **A contextual bandit learning from execution feedback** (Beta/Thompson bucketed by task features, rules as the cold-start prior), getting more accurate for similar tasks; offline benchmark reproducible (late rounds 89.6% vs rules 73.3%), off by default | None built in; you build the eval/fine-tune loop yourself | None built in | None built in | None built in |
| Multi-agent | **Explicitly not done** (belongs to the host layer) | Build it yourself | **Native strength** | Often supported | Platform-side multi-agent collaboration |
| Decision observability | One event-stream exit; every strategy choice/tool call/permission/capability-gap/tool-discovery has an event | External observability services or your own instrumentation | Framework logs/external services | Platform runtime logs | Built-in platform logs |
| Permission control | Host provides the PermissionPolicy SPI; read/write graded, writes can be human-authorized | Implemented by the app | Implemented by the app | Platform permission model | Platform model |
| Deployment requirements | Follows the host; an optional FastAPI battery and container example are provided | Deployed by the app | Deployed by the app | Must deploy and operate the platform | Just sign up |
| Offline testable | The full suite is offline (fake models/MockTransport/injected clocks), no network or keys | Testable, you mock yourself | Testable, you mock yourself | Depends on the platform running | Depends on the platform |
| Maturity | A new single-person project; 1,500+ tests, 10 example hosts | Mature ecosystems | Mature communities | Mature platforms | Commercial products |

## 3. YAI's differentiating claims (mapped to "technical innovation" scoring)

1. **A paradigm inversion: from "assembly framework" to "embedded kernel".** Existing frameworks share the
   premise "the developer first has an agent to build, then defines it". YAI answers a different question —
   "how does an already-existing piece of software (notes, CRM, a companion app…) gain agent capability
   without writing agent code?" The analogy is not an ORM but an embedded database: capabilities live in
   the app like data.
2. **Capability auto-discovery rather than manual registration.** Ordinary host Python functions become
   JSON-Schema tools via type hints + docstrings; external MCP servers and OpenAPI services integrate
   isomorphically, with zero awareness on the execution side.
3. **Adaptive routing + a capability-gap loop.** The kernel chooses among direct/react/plan/clarify per
   task (zero-cost rules as fallback, LLM as enhancement, auditable decision source). When tools cannot
   finish the task it first emits a `capability_missing` event; if a `ToolDiscovery` source is injected it
   discovers, registers and calls the missing capability **this turn** (emitting `tool_discovered`;
   discovery ≠ authorization) — turning "what capability am I missing" from a line of text into a runnable
   loop, instead of pretending a tool exists or calling blindly.
4. **The router self-calibrates from feedback (making "self-adaptive" real).** Keyword/LLM routing is
   open-loop: classify once and done. YAI optionally extracts success/failure from the event stream after a
   task, accumulates Beta(α,β) posteriors bucketed by task features, and uses Thompson Sampling to adjust
   strategy for similar tasks — deterministically following rules at cold start, only sampling once real
   feedback exists. Pure standard library, zero dependencies, offline-reproducible: the built-in
   five-line benchmark (including "no prior" and "no context" ablations) shows late-round success of 89.6%
   vs 73.3% for rules. Learning is off by default and abstains in hard-rule regions; the learning
   algorithm itself is the seventh SPI (`RouteSelector`) and is replaceable.
5. **Zero hard kernel dependencies + ten SPI contracts.** The kernel itself has `dependencies = []`;
   model/channel/memory/policy/discovery source/sandbox/route-learning/text-embedding/storage-cipher/resource are all replaceable,
   letting the kernel fit into mobile, desktop and restricted-network environments that cannot hold a
   framework's full dependency tree.
6. **Tool discovery is "dual channel", not keyword-based.** Keyword matching cannot recall a gap like
   "what should I wear outside, do I need a coat" that never says "weather". YAI's `SemanticCatalog`
   recalls through two independent channels — lexical (zero-dep, always available) and optional embedding
   semantics — computes cosine with stdlib `math` (no numpy in the kernel), and uses "an absolute threshold
   + the top-1 margin over multiple candidates" to avoid false recall among similar candidates. The
   embedding backend is pluggable (a free local model, or a cloud-compatible embedding API) and degrades to
   pure lexical on missing config or error; thresholds are calibrated against a real local model, not guessed.

## 4. Which category to pick (honest advice)

- **Visual dragging so non-technical teammates can build a Bot/knowledge base** → the self-hosted or
  hosted no-code platform categories; that is their home turf and YAI does not compete there.
- **Role division and multi-agent collaboration** → the multi-agent framework category; YAI explicitly
  does not do multi-agent.
- **Fine-grained graph orchestration, long-running stateful flows, human-approval nodes** → the low-level
  graph runtime category.
- **The largest ready-made integration surface and fast prototyping** → the general agent framework category.
- **Embed agent capability into existing software, keep it in-process, introduce no platform, and do not
  write your own agent loop** → that is exactly where **YAI Agent Core** sits.

> Names and links of alternatives are deliberately omitted here. When shortlisting concrete projects
> inside a category, search for them yourself and follow their official documentation.

## 5. Sources of fact

- YAI facts: this repository's `pyproject.toml` (dependencies/extras), `src/yai_core/`, `README.md`,
  `ROADMAP.md` (re-checked 2026-10-24; the test count comes from an actual `uv run pytest` run).
- Alternatives: described by category from public common knowledge; no third-party text, metrics or links.

> Category descriptions may lag behind product changes; treat each project's own current documentation
> as authoritative.
