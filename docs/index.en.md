> English translation of [中文](index.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# YAI Agent Core

**English** · [简体中文](index.md)

> An in-process embedded, **adaptive** Agent kernel — **the SQLite of the Agent world**.
> You do not need to deploy an agent platform, and you do not need to write Agent Loop / Planner / tool-selection code:
> `import` the kernel into the **software you already have**, the host only declares "what capabilities I have",
> and Core discovers capabilities, picks a strategy, calls tools, and completes the task.

- **Repository**: [github.com/Gi-Tuu/yai-agent-core](https://github.com/Gi-Tuu/yai-agent-core) (mirrored on Gitee)
- **Install**: `pip install yai-agent-core` · **License**: MIT · **Version**: v0.36.0
- **Zero third-party hard dependencies**: model, MCP, OpenAPI, browser/desktop, and OTel are all optional and lazily loaded

## Run it in 30 seconds

```bash
pip install yai-agent-core
```

```python
import asyncio
from yai_core import AgentCore, ScriptedModel, ModelResponse, ToolCallRequest, build_spec

def search_notes(keyword: str) -> list[str]:
    """Search the host's note store."""     # your own ordinary business function, with no Agent code
    return {"weekly": ["Core skeleton", "tool calls"]}.get(keyword, [])

model = ScriptedModel([                      # offline deterministic model; swap in OpenAICompatProvider() in production
    ModelResponse(content="", tool_calls=[ToolCallRequest(id="c1", name="search_notes",
                 arguments={"keyword": "weekly"})]),
    ModelResponse(content="2 weekly notes this week."),
])
core = AgentCore(model)
core.register_tools([build_spec(search_notes)])   # introspects type hints / docstring to build the tool
print(asyncio.run(core.run("Search the weekly notes and summarize")).final_text)   # 2 weekly notes this week.
```

The repository also ships a complete example with event streaming: `python examples/quickstart.py`.

## What to read next

| You want to… | Read this |
|---|---|
| Figure out all the docs and which one is authoritative | [Document map](DOCUMENT_MAP.md) |
| Know "how far it got, and where the evidence is" | [Capability and evidence index](IMPLEMENTATION_STATUS.en.md) |
| Understand the kernel layering and data flow | [Architecture](architecture.en.md) |
| Read the code in order (with line-by-line notes) | [Reading guide](reading-guide.md) → [Walkthrough overview](walkthrough/00-index.md) |
| Go to production: permissions, approval, deployment | [Production permissions guide](guides/production-permissions.en.md) · [Deployment handbook](guides/demo-deployment.en.md) |
| Do browser / desktop automation | [Browser extension and Bridge](guides/browser-extension-bridge.md) |
| See measured numbers (rather than slogans) | [Evaluations and evidence](evaluations/memory-evaluation.md) |
| See versions and changes | [Changelog](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CHANGELOG.md) |
| Compare with platforms / frameworks | [How it differs from peers](comparison.en.md) |

## What this is (the one-minute version)

- **Capability self-discovery**: the type hints + docstring of an ordinary Python function automatically
  become a JSON Schema tool; external MCP / OpenAPI plug in the same way.
- **Adaptive routing**: tasks automatically take `direct / react / plan / clarify`, and every decision is a
  visible event — replayable and auditable.
- **Capabilities on demand**: sense a capability gap → ask discovery sources for candidates → register →
  callable in this very turn.
- **Governance**: three permission tiers, argument-level approval, scope gates, execution receipt hash chain,
  trusted change previews.
- **Embeddable**: lives in the host process, no standalone service; ten SPI contracts, every part is replaceable.

For more detail read the [README](https://github.com/Gi-Tuu/yai-agent-core#readme) and the
[whitepaper](whitepaper.md).
