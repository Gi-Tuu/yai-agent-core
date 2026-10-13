# YAI Agent Core

**English** · [简体中文](README.md)

[![CI](https://github.com/Gi-Tuu/yai-agent-core/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Gi-Tuu/yai-agent-core/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/Gi-Tuu/yai-agent-core/badge)](https://scorecard.dev/viewer/?uri=github.com/Gi-Tuu/yai-agent-core)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/15360/badge)](https://www.bestpractices.dev/projects/15360)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Version](https://img.shields.io/badge/version-v0.9.0-blue.svg)](CHANGELOG.md)
[![Kernel](https://img.shields.io/badge/kernel-0%20third%20party%20deps-c0392b)](pyproject.toml)

<p align="center"><b>Give every piece of software a thinking heart.</b></p>

> An in-process, embeddable, **self-adaptive** Agent kernel — the **SQLite of the agent world**.
> You don't deploy an agent platform, and you don't write an Agent Loop / Planner / tool-selection layer:
> `import` the kernel into the software you **already have**. The host only declares *what capabilities it
> has*; Core discovers them, picks a strategy, calls tools, and finishes the task.

<p align="center">
  <img src="docs/assets/desktop-panel.png" alt="The YAI Dynamic Island employee panel: a chat-first surface with the reasoning chain and tool calls collapsed into process steps; a tax task runs end-to-end from tool discovery to archiving." width="520">
</p>

<p align="center"><b>One floating window is one digital employee.</b> Chat is the main surface; the reasoning
chain and tool calls collapse into "Process · N steps". Routing, capability gaps, newly discovered tools
flying in, and approval cards are all driven by real kernel events. All data shown is fictional.</p>

## What is this

YAI is not another chatbot, nor an agent framework you assemble by following a tutorial. It is a **kernel
that runs inside your software's process**:

- You hand it the **ordinary functions that already exist** in your software (or connect MCP / OpenAPI);
- It introspects those capabilities, auto-generates tool specs, and on each task **adaptively** decides
  whether to answer directly, call a tool, plan in steps, or ask you for clarification;
- When capabilities are insufficient it **senses the gap and discovers new tools on demand**; risky
  operations **request authorization by default** — and every step is visible as an event.

## Who it is / isn't for

| ✅ Good fit if you… | ⛔ Not a fit if you… |
|---|---|
| Already have working software and want to **add agent capability cheaply**, without platform lock-in | Want to **drag-and-drop agents** in a browser with no code (use a visual agent platform) |
| Want AI to call your software's **real functions**, not generic chit-chat | Want an out-of-the-box **cloud-hosted / team SaaS** agent |
| Need a **self-hostable, embeddable** agent runtime with swappable parts | Want an unattended, fully autonomous **coding agent** (YAI asks for approval by design) |
| Want to learn agent internals by building (auditable decisions, line-by-line walkthrough) | Expect a zero-config generic assistant — YAI's value is precisely in **binding to host capabilities** |

## How it differs from what you've heard of

|  | Platforms<br>visual agent platforms | Frameworks<br>orchestration · multi-agent frameworks | **Embedded kernel · YAI** |
|---|---|---|---|
| Form | Sign up, build inside the platform | Define agents / tools / orchestration yourself | **`import` into existing software** |
| Agent code you write | — | A lot | **None** |
| Deployment | Hosted by the platform | Run a service yourself | **Lives in the host process; no separate service** |
| Binding host capabilities | Weak, manual wiring | Manual wiring | **Automatic introspection & discovery** |
| Hard third-party kernel deps | — | Usually many | **0** (all optional, lazily loaded) |

> Database analogy: platforms are cloud database consoles, frameworks are ORMs, **YAI is SQLite** — it never
> steals the spotlight; it quietly embeds into your program.

## Running in 30 seconds

**Path A · Install the package only, verify embedding with zero API key** (no clone, no model signup). The
kernel itself has zero third-party hard dependencies:

```bash
pip install yai-agent-core
```

> To track unreleased commits on `main`, install from source instead:
> `pip install "yai-agent-core @ git+https://github.com/Gi-Tuu/yai-agent-core.git"`

```python
import asyncio
from yai_core import AgentCore, ScriptedModel, ModelResponse, ToolCallRequest, build_spec

def search_notes(keyword: str) -> list[str]:
    """Search the host's note database."""          # your own ordinary business function — no agent code
    return {"weekly-report": ["Core skeleton", "tool calls"]}.get(keyword, [])

# Offline deterministic model (swap the whole thing for OpenAICompatProvider in production):
# it calls the tool first, then gives the conclusion.
model = ScriptedModel([
    ModelResponse(content="", tool_calls=[ToolCallRequest(id="c1", name="search_notes",
                 arguments={"keyword": "weekly-report"})]),
    ModelResponse(content="2 items in the weekly report."),
])
core = AgentCore(model)
core.register_tools([build_spec(search_notes)])      # introspects type hints / docstring to build the tool
print(asyncio.run(core.run("Search the weekly report and summarize")).final_text)
```

Using a real model is a one-line change (`[llm]` extra; OpenAI-compatible DeepSeek / Qwen / OpenAI / Agnes):

```python
from yai_core import OpenAICompatProvider
model = OpenAICompatProvider()      # reads OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL
```

**Path B · Clone the repo and run every host and test** (offline, no key needed):

```bash
uv venv
uv pip install -e ".[dev,llm,server]"
uv run python scripts/smoke_test.py   # one Core adapting to several different hosts
uv run pytest                         # full test suite, no API key needed
```

New to the code? [`docs/reading-guide.md`](docs/reading-guide.md) is the learning path (zh-CN) and
[`docs/walkthrough/00-index.md`](docs/walkthrough/00-index.md) is the line-by-line walkthrough (zh-CN).
For architecture in English see [`docs/architecture.en.md`](docs/architecture.en.md) and
[`docs/comparison.en.md`](docs/comparison.en.md).

## Core capabilities

| Capability | In one line |
|---|---|
| **Capability auto-discovery** | Python type hints + docstrings auto-generate JSON Schema; external **MCP** tools (via MCP Client) and **OpenAPI** REST operations (via spec parsing) register the same way |
| **Adaptive routing** | Tasks route to `direct / react / plan / clarify`; LLM classification falls back to rules on error; insufficient tools emit `capability_missing`; every decision is auditable |
| **Self-calibrating learning** | A contextual bandit learns routing from success/failure and picks strategies via Thompson Sampling; offline benchmark reaches **89.6% vs 73.3% for rules (~+16pp)** in late rounds |
| **On-demand discovery loop** | Gap → ask discovery sources for candidates → register → **callable within the same turn**; dual-channel recall (lexical + local bge-m3 / cloud embeddings), and remote MCP/OpenAPI sources are conservatively narrowed by the gap too (fall back to all on no hit) |
| **Long-context compaction** | Over budget, old turns are summarized while recent turns stay intact; on model failure it falls back to hard truncation — long conversations keep early goals and facts |
| **Dynamic re-planning** | When a multi-step plan turns out unworkable mid-execution, the model calls `replan` to explicitly return to planning, keeping completed steps and producing a revised plan (`plan_revised`); registered only for PLAN, it only rearranges the internal plan with no side effects and no approval gate |
| **Cost-aware tiering** | Defaults to the cheap/fast `standard` tier and only escalates to `strong` for planning, failure-recovery, or being stuck in late rounds; tiering is deterministic and costs no extra call; DONE summarizes per-tier call counts; disable with `cost_aware=False` |
| **Structured long-term memory** | `remember` / `recall` proactively store and retrieve facts / preferences / profiles; near-duplicate merge, importance weighting, JSON persistence; no built-in vector memory — semantic recall goes through the Embedding SPI |
| **Safety boundaries** | Three permission tiers (approve-all / partial / no-approval); composed tools only orchestrate registered tools and never run generated code; code generation defines only the `ToolSandbox` contract — the sandbox is host-provided |
| **Sub-employee delegation** | The root employee can hand narrowed subtasks to short-lived sub-employees: tool allowlist ⊆ root, monotonically narrower permissions, an isolated small context, per-task budget and a global concurrency gate (≤5 in parallel, ≤2 levels deep); results flow back only to the root and the sub-employee is destroyed when done. The Dynamic Island switches between several mutually isolated specialists |
| **Fully observable** | Every strategy choice, capability gap, tool discovery/composition/call, and permission confirmation is emitted through one unified event stream |
| **Structured tracing** | The optional `[otel]` extra subscribes to the event stream from the outside and turns a run into one OpenTelemetry trace: a root span with a child span per tool (ok/error/duration) and adaptive decisions as span events; it never touches the kernel and never initializes an exporter — wiring Jaeger / Tempo / OTLP is the host's decision |
| **End-to-end evaluation** | A built-in offline eval harness runs the full Core with a ScriptedModel, quantifying completion rate, tool precision/recall, overreach and unsupported claims; golden regression baseline + non-blocking CI |

<details>
<summary>Show: the nine swappable SPI contracts</summary>

`Model · Channel · Memory · Policy · Discovery · Sandbox · RouteSelector · Embedding · StorageCipher`
— the host can replace any of them; Core provides zero-config defaults (`StorageCipher` is the optional
at-rest encryption slot). The kernel itself always has zero third-party hard dependencies.

</details>

## Two ways to use it

### 1) Embed as a library (three steps)

```python
from yai_core import AgentCore, OpenAICompatProvider
import my_app_capabilities as cap                       # the host's own ordinary business functions

core = AgentCore.auto(cap, OpenAICompatProvider())      # ① introspect capabilities, auto-register tools
result = await core.run("Search this week's records and compile a report")  # ② adaptive direct/react/plan/clarify
print(result.final_text)                                # ③ deliverable result + full event trace
```

### 2) The Dynamic Island employee thin shell (`shell/desktop`)

Hosts that want a "face" **don't have to write their own chat window**: the official thin shell provides a
persistent floating window, rendered natively with Qt Quick, running in the **same process** as the Core
embedded in each app (no local port).

<p align="left">
  <img src="docs/assets/desktop-ball.png" alt="The Dynamic Island guide ball: a round ball with a pulsing purple ring and a 'click me' hint." width="180">
</p>

- **Three-state morphing**: round ball (56px) → on hover expands right into a capsule (340×52) → on click
  expands down into the employee panel (470×620); draggable throughout;
- **Position memory**: after dragging, closing and reopening returns the window to where it was;
- **First-run guide**: a "click me" hint + pulsing purple ring, shown once and never again;
- **Global hotkey**: press `Ctrl+Alt+Y` in any application to summon and expand it instantly;
- **Offline / live dual entry**: desktop shortcuts start either the offline demo or the live-model employee;
- **Multi-specialist switching**: one island manages several **isolated** specialists — sales / warehouse /
  notes / companion — without losing timelines. The island is only the front desk / switchboard, **not a
  super-agent**.

```bash
uv sync --extra desktop
.\.venv\Scripts\python.exe -m shell.desktop            # offline demo: all animations, no API key
.\.venv\Scripts\python.exe -m shell.desktop --live     # connect a real model
```

The shell only renders and orchestrates. `shell/employee` implements sub-workers as one-shot sub-runs with
"narrowed tools + narrowed permissions + isolated context + a budget" (≤ 5 in parallel, ≤ 2 levels deep,
results flow back only to the root); **the kernel knows nothing about the UI and is not a multi-agent
framework**.

## Screenshots

<p align="center">
  <img src="docs/assets/hoste-native.png" alt="The sales CRM's six native views, fully usable without Core." width="45%">
  &nbsp;&nbsp;
  <img src="docs/assets/hoste-discovery.png" alt="The CRM with Core embedded: it auto-discovers a weather tool when capabilities are insufficient and requests authorization." width="45%">
</p>

<p align="center"><b>Left:</b> the app as it naturally is (six native views, fully usable without Core);
<b>right:</b> after embedding Core, it discovers tools when capabilities are short, and still shows an
approval card on first call (discovery ≠ authorization).</p>

<p align="center">
  <img src="docs/assets/router-learning-curve.svg" alt="The self-calibrating router learning curve: the bandit climbs from the rules baseline to about 90%." width="60%">
</p>

## Examples (from simplest to deepest)

| # | Host | What to see |
|---|---|---|
| 1 | `host_a_notes` | Minimal integration: ordinary functions become tools automatically |
| 2 | `host_c_companion` | Changing domain = changing job (an AMBRACE rehearsal) |
| 3 | `host_d_mcp` | Connecting to an external MCP Server as a Client |
| 4 | `host_f_discovery` | Capability gap → discovery → usable this turn (static / semantic) |
| 5 | `host_g_sandbox` | Teaching sandbox: build and run a code tool on the spot |
| 6 | `host_e_sales_crm` | Flagship: a standalone CRM that embeds Core with one switch (terminal + web, port 8200) |
| 7 | `host_i_mealie` | Adapting a real third-party open-source project over REST (Mealie) |
| 8 | `host_j_actual` | Third-party seeding "five-beat" line in the Actual Budget ledger app (discover a missing tool / delegate sub-employees; web on 8240) |

> Directory names keep their historical letters (a/c/d/e/f/g/i/j) and are not reordered by learning
> sequence, so they stay consistent with the docs and walkthrough paths.

## Online HTTP API

Live instance: **https://yai-agent-core.onrender.com** (free tier cold start ~30–90s)

```bash
GET  /health                  # {"status":"ok", ...}
GET  /v1/tools                # the tool list the current host auto-discovered
POST /v1/agent/run            # {"task": "..."} -> event stream + final result
```

For optional environment variables (persistent memory, history trimming / TTL, rate limiting) see
[`.env.example`](.env.example) and [`docs/architecture.en.md`](docs/architecture.en.md).

## Version roadmap

- **v0.1 – v0.3 (done)**: function introspection, rules/LLM routing, Agent Loop, default SPI
  implementations, MCP Client, SQLite persistence, OpenAPI discovery, history trimming & rate limiting,
  containerized deployment
- **v0.4 – v0.6 (done)**: capability-gap sensing and the ToolDiscovery loop, zero-change embedding of the
  host_e CRM, three permission tiers, composed tools, dual-channel semantic discovery, in-execution
  discovery
- **v0.7 (done)**: contextual-bandit self-calibrating router (+16pp offline), code-tool
  registration/TTL/approval gate, subprocess teaching sandbox, the Dynamic Island employee shell
- **v0.8 (done)**: context compaction; MCP/OpenAPI dynamic discovery sources with multi-source
  aggregation; SQLite KV TTL; Docker sandbox adapter; API-key auth, rate limiting and response guards;
  published to PyPI; CI hardening (CodeQL, OpenSSF Scorecard, mypy, coverage gate)
- **v0.9 (in progress, already on main)**: dynamic re-planning `replan` and cost/latency-aware `tiering`
  (automatic standard/strong shifting + DONE cost summary); semantic recall of long-term memory with
  proactive recall at task start, and the 9th SPI `StorageCipher` for at-rest encryption; structured
  OpenTelemetry tracing, metrics/readiness and a task-level budget circuit breaker; sub-employee budget
  and a global concurrency gate; and the third-party seeding five-beat line (`host_j` embeds into the
  real bookkeeping app Actual Budget with zero source changes)
- **v1.0 (planned)**: flow back in as the agent kernel of the AI Companion project
  [AMBRACE](https://github.com/Gi-Tuu/AMBRACE)

## License

MIT · [Gi-Tuu](https://github.com/Gi-Tuu)
