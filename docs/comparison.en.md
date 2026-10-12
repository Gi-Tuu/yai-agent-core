# YAI Agent Core vs. Alternatives (an honest comparison)

[English](comparison.en.md) · [简体中文](comparison.md)

> Last updated: 2026-10-10. This document states only verifiable facts: YAI's side is based on this
> repository's code/config, and each alternative on its official documentation. No disparaging
> comparisons; the end tells you "who to pick in which scenario".

## 1. One-line positioning

| Project | Official self-positioning (fact) | Form |
|---|---|---|
| **YAI Agent Core (this project)** | An in-process, embeddable, self-adaptive Agent **kernel**: the host only declares capabilities; the kernel auto-discovers tools, routes adaptively and executes | A Python library (`pip install`); the kernel itself has zero third-party hard dependencies |
| LangChain | "The fastest way to build agents", with a standard tool-calling architecture, vendor-agnostic design and middleware | Python/JS framework |
| LangGraph | LangChain's official **low-level orchestration framework/runtime**, for precise control and production-grade long-running agents | Python/JS framework |
| CrewAI | A **role-based multi-agent orchestration framework** for quickly building role-divided agent workflows | Python framework |
| AutoGen / Microsoft Agent Framework | Microsoft's **multi-agent conversation framework** (Agent Framework is the unified successor of AutoGen + Semantic Kernel) | Python/.NET framework |
| Dify | An open-source LLM-application **platform**: Backend-as-a-Service + LLMOps, visual Workflow/Chatflow editor, self-hosted via Docker | Self-hosted platform (server + web console) |
| Coze | An AI-application **platform** for users of all skill levels, visual no-code/low-code orchestration, hosted and operated | Hosted SaaS (Coze China / Coze global) |

## 2. Comparison across key dimensions

| Dimension | YAI Agent Core | LangChain / LangGraph | CrewAI / AutoGen | Dify | Coze |
|---|---|---|---|---|---|
| Delivery form | An imported library running in the host process | Library/framework | Library/framework | Self-hosted server platform | Hosted SaaS |
| What the developer does | Write ordinary business functions and declare capabilities; **no Agent Loop/Planner/tool selection** | Define agents, tools, chains/graphs, states and edges yourself | Define roles, tasks, collaboration flows yourself | Configure prompts, tools, workflows in the platform | Drag-and-drop a Bot/plugin on a web page |
| Embedding into existing software | In-process `import`, one-line `AgentCore.auto`; no server introduced | Embeddable, but you must implement agent assembly and the loop yourself | Embeddable, but the multi-agent paradigm needs app cooperation | The app becomes a Dify client/external caller | Cannot embed; the app runs inside the platform |
| Kernel runtime deps | **`dependencies = []`** (openai/fastapi/mcp/httpx are all optional, lazily-loaded extras) | A set of layered dependency packages | Framework dependencies | A full server stack (multi-container Docker Compose) | None (hosted) |
| Tool onboarding | Function introspection auto-registration; **MCP Client**; **OpenAPI auto-discovery**; **on-demand ToolDiscovery loop** | Tool abstractions + many integration packages | Tool abstractions | In-platform tools/plugin marketplace | Platform plugin ecosystem |
| Tool discovery/matching | **Dual channel: lexical (zero-dep, Chinese bigrams, synonyms) + optional embedding semantics**, stdlib cosine, two-layer summaries, margin to prevent false recall; degrades to pure lexical with no embedder configured | Usually manual binding by the developer or stuffing everything into the prompt; no built-in semantic retrieval | None built in | Fixed in-platform toolset, chosen by name | Platform plugin marketplace |
| Orchestration paradigm | Adaptive routing (direct/react/plan/clarify) + a minimal capability-gap loop (sense→discover→register→usable this turn); the host presets no flow | Chains/middleware (LangChain); graphs and state machines (LangGraph) | Role teams / conversation-driven | Visual workflow graph | Visual orchestration |
| Routing self-calibration | **A contextual bandit learning from execution feedback** (Beta/Thompson bucketed by task features, rules as the cold-start prior), getting more accurate for similar tasks; offline benchmark reproducible (late rounds 89.6% vs rules 73.3%), off by default | None built in; you build the eval/fine-tune loop yourself | None built in | None built in | None built in |
| Multi-agent | **Explicitly not done** (belongs to the host layer) | Build it yourself | **Native strength** | Supports multiple agents | Native platform multi-agent collaboration |
| Decision observability | One event-stream exit; every strategy choice/tool call/permission/capability-gap/tool-discovery has an event | Relies on external observability such as LangSmith | Framework logs/external services | Platform runtime logs | Built-in platform logs |
| Permission control | Host provides the PermissionPolicy SPI; read/write graded, writes can be human-authorized | Implemented by the app | Implemented by the app | Platform permission model | Platform model |
| Deployment requirements | Follows the host; an optional FastAPI battery and Docker example are provided | Deployed by the app | Deployed by the app | Must deploy and operate the platform | Just sign up |
| Offline testable | The full suite is offline (fake models/MockTransport/injected clocks), no network or keys | Testable, you mock yourself | Testable, you mock yourself | Depends on the platform running | Depends on the platform |
| Maturity | A new single-person project; 517 tests, 7 example hosts | Mature large ecosystem | Mature community | Mature platform (156K+ stars) | Commercial product |

## 3. YAI's differentiating claims (mapped to "technical innovation" scoring)

1. **A paradigm inversion: from "assembly framework" to "embedded kernel".** Existing frameworks share the
   premise "the developer first has an agent to build, then defines it". YAI answers a different question —
   "how does an already-existing piece of software (notes, CRM, a companion app…) gain agent capability
   without writing agent code?" The analogy is not an ORM but SQLite: capabilities live in the app like
   data.
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
5. **Zero hard kernel dependencies + nine SPI contracts.** The kernel itself has `dependencies = []`;
   model/channel/memory/policy/discovery source/sandbox/route-learning/text-embedding/storage-cipher are all replaceable,
   letting the kernel fit into mobile, desktop and restricted-network environments that cannot hold a
   framework's full dependency tree.
6. **Tool discovery is "dual channel", not keyword-based.** Keyword matching cannot recall a gap like
   "what should I wear outside, do I need a coat" that never says "weather". YAI's `SemanticCatalog`
   recalls through two independent channels — lexical (zero-dep, always available) and optional embedding
   semantics — computes cosine with stdlib `math` (no numpy in the kernel), and uses "an absolute threshold
   + the top-1 margin over multiple candidates" to avoid false recall among similar candidates. The
   embedding backend is pluggable (local bge-m3, offline and free, or an OpenAI-compatible cloud) and
   degrades to pure lexical on missing config or error; thresholds are calibrated against real bge-m3, not
   guessed.

## 4. Who to pick in which scenario (honest advice)

- You want to **drag-and-drop and let non-technical teammates build a Bot/knowledge base** → pick
  **Dify / Coze**; that is their home turf and YAI does not compete there.
- You want **role division and multi-agent collaboration** → pick **CrewAI / AutoGen (Microsoft Agent
  Framework)**; YAI explicitly does not do multi-agent.
- You want **fine-grained graph orchestration, long-running stateful flows, human-approval nodes** → pick
  **LangGraph**; graphs are its abstraction strength.
- You want **fast prototyping with the richest integration ecosystem** → pick **LangChain**.
- You want to **embed agent capability into existing software, keep it in-process, introduce no platform,
  and not write your own agent loop** → that is exactly where **YAI Agent Core** sits.

## 5. Sources

- YAI facts: this repository's `pyproject.toml` (dependencies/extras), `src/yai_core/`, `README.md`,
  `ROADMAP.md` (re-checked 2026-10-10).
- LangChain / LangGraph: official docs https://docs.langchain.com/oss/python/concepts/products ; the 1.0
  release notes https://www.langchain.com/blog/langchain-langgraph-1dot0
- CrewAI / AutoGen: LangChain's official framework roundup (2026-06)
  https://www.langchain.com/resources/ai-agent-frameworks
- Dify: site https://dify.ai/ ; docs https://docs.dify.ai/
- Coze: https://docs.coze.com/guides ; https://docs.coze.cn/what_is_coze

> These projects iterate quickly; the above reflects their official pages as of 2026-09-20. Before
> submitting, if a project's version/positioning has changed, defer to its latest official docs.
