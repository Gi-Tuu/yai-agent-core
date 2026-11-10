> English translation of [中文](reading-guide.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# YAI Agent Core Code Reading Guide (for learning by building)

**English** · [简体中文](reading-guide.md)

> Goal: not "understand every line", but after reading in order, be able to explain clearly to someone else how the Core runs, and be able to change it yourself.
> Suggested pace: 1–2 stops a day, together with running things and small exercises; if you are stuck for more than 30 minutes, skip ahead for now and leave a `# Q:` comment in the file.

> **Companion line-by-line walkthrough**: [`docs/walkthrough/`](walkthrough/00-index.md) explains every source file block by block and line by line (00 overview → 56 v0.26 execution adapter); this document owns the route, the walkthrough owns the fine detail — read the two side by side. For the full document landscape see [`docs/DOCUMENT_MAP.md`](DOCUMENT_MAP.md).

## 0. Get it running first (10 minutes)

```powershell
cd "E:\Competition\YAI Agent Core"
.\.venv\Scripts\python.exe scripts\smoke_test.py
```

Observe the difference between the two hosts' output: **the same Core code is untouched**; only `capabilities` changed, and the auto-discovered tools and the chosen strategy differ. That is the core claim of the whole project — build an intuitive feel for it first.

## 0.5 Order of the example hosts (when you want to look at examples directly)

The 11 example hosts (including 1 production shape) go from shallow to deep (consistent with README and IMPLEMENTATION_STATUS):

1. `host_a_notes` — minimal integration: ordinary functions automatically become tools
2. `host_c_companion` — changing the domain changes the job (AI companion / AMBRACE rehearsal)
3. `host_d_mcp` — connecting to external MCP Servers as a Client
4. `host_f_discovery` — capability gap → discover on demand (static / semantic)
5. `host_g_sandbox` — build code tools on the spot inside the sandbox and execute them
6. `host_e_sales_crm` — full product: embed the Core with one switch, including self-learning and a web page
7. `host_i_mealie` — adapting a real high-star third-party open-source project
8. `host_j_actual` — the real bookkeeping app Actual Budget: discover to fill capability gaps / dispatch sub-agents
9. `host_k_multimodal` — multimodal: the model only selects an image handle; the host decides scope / lifetime / size
10. `host_l_sidecar_node` — cross-language: a zero-dependency Node process integrates through the sidecar protocol
11. `host_production` — production shape: a complete host with API Key auth / rate limiting / readiness probe

> Directory names keep their historical numbering (a/c/d/e/f/g/i/j/k/l) and `host_production`; they are not reordered to follow the learning order, so that paths stay consistent with the material of each release and with the walkthrough.

## 1. Recommended reading order (7 stops)

### Stop 1: `src/yai_core/types.py` — the project's "vocabulary"
- What it solves: what the data passed around the whole project looks like (tools, messages, events, strategies).
- Python knowledge points: `dataclass` (data container), `StrEnum` (string enum), `Literal`, type annotations, `field(default_factory=...)`.
- Done when: you can say what `ToolSpec / ChatMessage / ModelResponse / AgentEvent` each of them holds.
- Small exercise: add a `RETRY = "retry"` to `EventType`, and think about which part would emit it.

### Stop 2: `src/yai_core/discovery/introspect.py` — where the "capability self-discovery" magic comes from
- What it solves: how a host's ordinary functions automatically become tools with a JSON Schema.
- Python knowledge points: `inspect.signature`, how type annotations are read by code, `get_origin/get_args` (how Optional works), dictionary construction.
- Key insight: there is no magic here; it just "reads" the function signature and translates it into a format the LLM understands.
- Small exercise: add a new function with a default argument to `examples/host_a_notes/capabilities.py`, re-run smoke, and see whether it shows up automatically.

### Stop 3: `src/yai_core/tools/` — the registry and the execution bus
- `registry.py`: Python knowledge points — classes, wrapping a dict, custom exceptions (when to use `KeyError/ValueError`).
- `executor.py`: focus on three things — the permission decision, how `inspect.isawaitable` lets both sync and async functions run, and why try/except "feeds back" tool errors to the model instead of crashing.
- Small exercise: deliberately change a tool function to `raise ValueError("boom")` and observe whether the Agent can still finish cleanly.

### Stop 4: `src/yai_core/kernel/router.py` — the first version of adaptive decision making
- What it solves: on what basis direct/react/plan/clarify is chosen.
- Python knowledge points: enum comparison, `any()/sum()` generators, pure functions (same input always gives the same output, so they are the easiest to test).
- Read alongside the tests: each case in `tests/test_router.py` is a "spec sheet" for one decision rule.
- Small exercise: add your own Chinese trigger word so that "帮我看看" ("take a look for me") also triggers react; add a test at the same time.

### Stop 5: `src/yai_core/kernel/loop.py` — the heart of the Core (most important, read slowly)
- What it solves: after a strategy is selected, how the model and the tools loop back and forth until a result is produced.
- Python knowledge points (exactly your study list):
  - `async def / await`: why IO-heavy LLM calls must be async;
  - **async generators** `async ... yield`, `async for`: why events can be "spat out while running";
  - control flow: plan splits the steps first, react loops, max_iters is the safety net, clarify recurses back in.
- Debugging tip: print `len(ctx.messages)` at the start of each loop iteration in `_react_cycle` to see how the message history grows.
- Done when: you can draw from memory the closed loop "user task → model → tool call → tool result → model → final answer".

### Stop 6: `src/yai_core/spi/` and the default implementations — replaceable slots (ten SPI contracts)
- What it solves: why the Core is not tied to a model vendor, a UI, or a database.
- Python knowledge points: `Protocol` (structural subtyping: "as long as it looks like this it can plug in", no inheritance needed), dependency injection (passed in through the constructor).
- Read alongside: `llm/openai_compat.py` (a real slot), `channels/collect.py` (a test slot), `ScriptedModel` in `tests/test_loop.py` (a fake slot).
- Small exercise: write an `EchoModel` that implements `achat` (returning a fixed line), plug it into the Core and run it once.

**One more stop (v0.2)**: `src/yai_core/memory/sqlite_store.py` — the second implementation of the same memory slot.
Read it against InMemoryStore: table structure, scope isolation, the user_version migration hook, and why synchronous SQL runs directly inside
async methods (see walkthrough chapter 11). Afterwards you should be able to answer: why did the Loop gain persistent memory without a single line changed?

**Yet another stop (v0.2)**: `src/yai_core/integrations/openapi/` — read it against walkthrough chapter 10 (MCP bridge):
spec.py's $ref inlining and cycle protection, discovery.py's parameter merging, client.py's authentication and response normalization.
Afterwards you should be able to answer: why a single OpenAPI description lets the Core gain a batch of tools without writing one line of adapter code (see walkthrough chapter 12).

**Yet another stop (v0.6)**: `src/yai_core/spi/discovery.py` + `discovery/catalog.py` — the fifth slot, "discover on demand".
Read it against walkthrough chapter 15: the `ToolDiscovery` contract, `StaticCatalog`'s keyword matching, and why "discovery sources only supply candidates; registration/deduplication/permissions belong to the kernel".
Afterwards you should be able to answer: why a capability gap can be filled **in the same round** and then called, rather than just emitting an event; run `examples/host_f_discovery/run.py` (no Key needed).

### Stop 7: `src/yai_core/core.py` and `batteries/fastapi_server/app.py`
- `core.py` is the facade: a host only needs to know `AgentCore.auto()` and `run()`; everything inside is hidden (facade pattern).
- The Battery shows the boundary between "kernel" and "periphery": note that fastapi/pydantic are **lazy-loaded**; the kernel itself does not depend on them.
- Small exercise: following the style of `scripts/serve_example.py`, turn host_c_companion into an HTTP service too.

## 2. Glossary (you will meet these again and again on a first read)

| Term | What it means in this project |
|---|---|
| Host | The existing software that embeds the Core (11 hosts under `examples/`, including 1 production shape; in the future, AMBRACE) |
| SPI | Interface contracts that the host can replace and for which the Core provides default implementations (Model / Channel / Memory / Policy / Discovery / Sandbox / RouteSelector / Embedding / StorageCipher / ResourceProvider — ten in total) |
| Adaptive Router | The component that decides which execution strategy a task takes |
| ReAct | The loop of reasoning → calling tools → observing results → reasoning again |
| Tool Bus | The unified channel for executing tools (permissions, exceptions and result formatting all live here) |
| Observer event stream | Observable events emitted outward during a run; adaptive decisions are not allowed to be a black box |
| Battery | Optional peripheral capabilities (HTTP service, etc.); not part of the kernel |

## 3. Hands-on experiment environment

- It is recommended to open `E:\Competition\YAI Agent Core` in VS Code, click to the left of a line number in `loop.py` to set a breakpoint, and press F5 to debug `scripts/smoke_test.py`.
- Common commands:
  ```powershell
  .\.venv\Scripts\python.exe -m pytest            # all tests
  .\.venv\Scripts\python.exe -m pytest -k router  # only the router tests
  .\.venv\Scripts\python.exe -m ruff check src tests examples scripts
  ```

## 4. "I've got it" self-check list

- [ ] Can draw the Core's layered diagram without looking at the code and name the files in each layer
- [ ] Can explain why hosts have "zero Agent code"
- [ ] Can say which roles appear in the messages list, in order, during one react loop
- [ ] Can add a host function and get the Agent to call it
- [ ] Can write a fake ModelProvider to test a new execution path offline
- [ ] Can point out which part of the code is the boundary where "the kernel must never depend on third parties"

Once all the boxes are ticked, move on to Day 1-3 (real DeepSeek integration) — by then you will know exactly what every line is doing.
