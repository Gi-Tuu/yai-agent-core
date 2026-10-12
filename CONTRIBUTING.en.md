# Contributing to YAI Agent Core

**English** · [简体中文](CONTRIBUTING.md)

Thanks for your interest in YAI Agent Core. It is an **in-process, embeddable, self-adaptive Agent
kernel**: the host software only declares ordinary business functions, and Core automatically turns those
functions into tools, adaptively chooses an execution strategy, and "shouts out" the whole process as an
event stream. Before you start, read the positioning section of [README.en.md](README.en.md) — we are not
building yet another agent framework that developers have to assemble themselves.

This document is also the maintainer's standard operating handbook: how to set up the environment, where
code goes, and which checks must pass before a commit.

## 0. Go green in 30 minutes

No API key is required. Run these in order:

```bash
# Prerequisites: Python 3.11+, uv (https://docs.astral.sh/uv/), git
git clone https://github.com/Gi-Tuu/yai-agent-core.git
cd yai-agent-core
uv venv
uv sync --extra dev --extra llm --extra server --extra mcp --extra openapi   # install everything at once
uv run pytest                                   # ① the full offline suite should be all green
uv run python scripts/smoke_test.py             # ② one kernel runs smoke scenarios across three hosts
uv run python examples/host_e_sales_crm/standalone_cli.py   # ③ the native CRM menu without AI ("the app as it is")
```

Then read these three documents in order:

1. `README.en.md` — positioning (embedded kernel vs platform/framework) and the 30-second example;
2. `docs/reading-guide.md` — the code-learning path (zh-CN);
3. `docs/walkthrough/00-index.md` — a line-by-line explanation of every source file (start at 00, don't
   skip around; zh-CN).

To try a real model: copy `.env.example` to `.env`, fill in a key, then run
`examples/host_a_notes/run.py`.
To contribute: read the "Architecture red lines" (§2) below, then start from issues labeled
`good first issue`.

## 1. Development environment

Python 3.11+ is required (development and CI target 3.13); use [uv](https://docs.astral.sh/uv/) for
package management.

```bash
# 1. Create and activate a virtual environment
uv venv
.venv\Scripts\activate            # Windows PowerShell; on Linux/macOS: source .venv/bin/activate

# 2. Install: editable mode + dev/live-model/server extras (add the matching extra for MCP/OpenAPI)
uv pip install -e ".[dev,llm,server]"
uv pip install -e ".[mcp]"          # only when developing/running the MCP Client integration
uv pip install -e ".[openapi]"      # only when developing/running OpenAPI discovery (httpx + pyyaml)
# Everything at once: uv sync --extra dev --extra llm --extra server --extra mcp --extra openapi

# 3. Verify: offline tests and smoke, no API key at any point
#    (with the venv active you can run pytest directly; otherwise use uv run pytest)
uv run pytest
uv run python scripts/smoke_test.py
```

The suite currently has **517 tests (8 skipped, 0 failures)**; the number grows over time, so the live
`pytest` output is authoritative. Tests are fully offline and skip only when an environment piece is
missing (no Docker daemon, no local bge-m3 weights, etc.).

To run against a real model (an OpenAI-compatible endpoint such as DeepSeek), copy `.env.example` to
`.env` and fill in a key:

```bash
cp .env.example .env              # Windows: copy .env.example .env
```

`.env` is in `.gitignore` — **never commit an API key to the repository**.

## 2. Architecture red lines (read before changing anything)

1. **The kernel itself has zero third-party hard dependencies.** `pyproject.toml`'s `dependencies` must
   stay empty; openai, fastapi, mcp, etc. may only appear as optional extras (`llm` / `server` / `mcp` /
   …) and be **lazily imported** inside their modules (top-level `integrations/mcp/` must not import
   `mcp`; an AST test enforces this).
2. **All external capabilities go through SPI contracts** (`src/yai_core/spi/`). The nine slots —
   model, channel, memory, policy, discovery, sandbox, route-selector, embedding and storage cipher —
   must be replaceable; Core depends only on `Protocol`s, never on concrete implementations.
3. **The event stream has exactly one exit.** Process events such as tool execution are collected by the
   executor, yielded uniformly by `AgentLoop`, and `AgentCore.astream` is the single outward emit point.
   No second event path is allowed.
4. **Every adaptive decision must be observable.** Any new branch where "Core decides on its own" must
   emit a corresponding `AgentEvent`; silent decisions are not allowed.
5. **Kernel boundaries.** No MCP Server, no peer-to-peer multi-agent orchestration, no in-kernel execution
   of generated code, no vector long-term memory, no built-in UI, no coding agent. Some of these are
   provided by the shell layer or optional extras; see §7 of
   [`docs/architecture.en.md`](docs/architecture.en.md) for the precise, current boundary. Open a GitHub
   issue for related proposals first.

## 3. Code style

- Linter / import sorting: [Ruff](https://docs.astral.sh/ruff/), rule set in `pyproject.toml`
  (`E,F,I,UP,B`), line width 100.
- These must pass locally before committing:

  ```bash
  ruff check src tests examples scripts
  pytest
  ```

- Type annotations are mandatory: public APIs must annotate parameters and return values; prefer
  `@dataclass` for data structures and `StrEnum` for enums. CI also runs `mypy` and enforces a coverage
  gate (≥ 90%).
- Start every module with a docstring stating its place in the overall picture; for learners, add inline
  comments on complex logic blocks.
- **Changing source means updating the walkthrough**: `docs/walkthrough/` maps block-by-block to the
  code; when behavior changes, update the matching chapter in the same PR (the walkthrough is currently
  in Simplified Chinese).

## 4. Testing conventions

- Tests **must not depend on the network, API keys, or external services**. Verify real protocols with
  fake modules / fake endpoints — see `tests/test_openai_compat.py` (injects a fake `openai` module),
  `tests/test_loop.py` (ScriptedModel), and `tests/test_mcp_bridge.py` (connects directly to an in-memory
  MCP server instance: no subprocess, no network; a `pytest.importorskip` on the first line keeps the
  minimal environment working).
- New features must ship with tests: each new strategy, tool source, or SPI implementation gets at least
  one end-to-end case.
- For a bug fix, first write a failing test that reproduces the bug, then make it pass.

## 5. Common development tasks

| You want to… | Touch | Reference |
|---|---|---|
| Make a new piece of software adaptable by Core | Create `examples/host_x/` with only ordinary functions + a run script | `examples/host_a_notes/` |
| Support a new model vendor | Implement the `ModelProvider` contract (reuse the OpenAI-compatible provider when possible) | `src/yai_core/llm/`, `spi/model.py` |
| Add a tool source (MCP/OpenAPI) | Produce unified `ToolSpec`s registered in `ToolRegistry`; the execution side is unchanged | `integrations/mcp/client.py`, `discovery/introspect.py` |
| Add an "on-demand discovery" source | Implement the `ToolDiscovery` contract (`discover(need, *, task, available) -> list[ToolSpec]`), inject via `AgentCore(..., discovery=)`; it only returns candidates — registration/dedup/permissions stay in the kernel | `spi/discovery.py`, `discovery/catalog.py`, `tests/test_discovery_catalog.py` |
| Add an execution strategy | Add deterministic rules in `AdaptiveRouter` with tests first (LLM classification is already implemented and falls back to these rules) | `kernel/router.py` |
| Swap memory / permissions / IO | Implement the matching SPI, inject via `AgentCore(..., memory=/policy=/channel=)` | `src/yai_core/spi/` |

## 6. Commit and PR workflow

- Branch names: `feat/xxx`, `fix/xxx`, `docs/xxx`, `test/xxx`, `chore/xxx`.
- Commit messages follow Conventional Commits, e.g. `feat: wire host_a to real DeepSeek tool calls`,
  `fix: consume line-form messages produced by Context in the provider`, `docs: add chapter 06 loop
  walkthrough`.
- One PR solves one thing; keep commits small and don't mix refactors with features.
- The PR description must state: what changed, why, how it was verified (paste commands and results), and
  whether the public API or the walkthrough changed.
- Pre-merge checklist: `ruff check` passes, `pytest` is green, new behavior has tests, docs are updated,
  no keys/private data leaked.

## 7. Issues and discussion

- Bug reports should include: OS and Python version, reproduction command, expected vs actual result, and
  the full error traceback; for model issues note the vendor, model name, and whether the offline tests
  pass.
- Feature requests should first describe the **host scenario and pain point** (which software, what agent
  code you'd otherwise have to write) before proposing a solution. Requests that don't fit the "embedded
  self-adaptive kernel" positioning may be suggested for a Battery or a separate repository.

## 8. License

This project is released under the MIT license. Opening a PR means you agree your contribution is
published under MIT.
