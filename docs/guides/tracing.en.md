# Structured Tracing (OpenTelemetry)

**English** · [简体中文](tracing.md)

YAI maps one Agent run onto a single [OpenTelemetry](https://opentelemetry.io/) trace, so "which strategy the model picked, which tools it called, which one failed, where it got stuck" is visible at a glance in Jaeger / Tempo / Grafana / any OTLP backend.

It is an **outside subscriber** — an optional integration, not part of the kernel:

- The kernel itself still has **zero third-party hard dependencies**; all of OpenTelemetry lives in the optional `[otel]` extra and is lazy-loaded;
- **It does not change a single line of `kernel/` or `core.py`** — tracing simply subscribes to the `AgentCore.astream` event stream like any other host;
- The library **never initializes** a TracerProvider / Exporter. Whether to report and where is entirely the host's decision.

## 1. Install

```bash
uv pip install -e ".[otel]"
# or from PyPI: pip install "yai-agent-core[otel]"
```

Without it the whole package still imports; only when you actually start a trace will an `ImportError` with install guidance be raised.

## 2. Two entry points

### 2.1 `traced_run`: get the result and a trace in one call

```python
from yai_core.integrations.tracing import traced_run

result = await traced_run(core, "Search this week's notes and summarize them")
# result is identical to core.run(...): strategy / events / final_text
# a trace is produced as well (using the global TracerProvider)
```

### 2.2 `traced_astream`: render a stream while recording

Use it when you forward events to a UI in real time (the Dynamic Island / web). Every event is forwarded **unchanged**, while the trace is recorded quietly on the side:

```python
from yai_core.integrations.tracing import traced_astream

async for event in traced_astream(core, "Organize this week's notes"):
    await websocket.send_json(event.model_dump())   # identical to core.astream
```

Both accept `tracer=` and `root_attributes=`:

```python
result = await traced_run(
    core,
    "task",
    tracer=my_tracer,                       # defaults to the global provider's tracer
    root_attributes={"yai.host": "notes", "yai.env": "local"},
)
```

## 3. Span model

A run is a two-level span tree:

```
yai.agent.run                         (root span, one per task)
├── yai.tool.recall_memories          (one child span per tool call)
├── yai.tool.search_notes
└── yai.tool.save_memory
```

### Root span: `yai.agent.run`

| Attribute / event | Meaning |
|---|---|
| attribute `yai.task` | Raw task text (truncated to 2000 chars) |
| attribute `yai.strategy` | Final strategy `direct / react / plan / clarify` |
| attribute `yai.tier` | Most recent model tier `standard / strong` |
| attribute `yai.final_text_len` | Length of the final reply (the body itself is not attached, avoiding large text / privacy in traces) |
| attributes `yai.host`, etc. | Passed through via `root_attributes`, to tell host applications apart |
| span event `yai.<event-type>` | Strategy choice, plan created/revised, clarify, capability gap, tool discovery/composition/creation, context compaction, tier change, etc.; event fields become `yai.<key>` event attributes |
| Status | `ERROR` if the stream raises or an `error` event arrives (the exception is also recorded via `record_exception`); otherwise `OK` |

### Tool child span: `yai.tool.<name>`

| Attribute | Meaning |
|---|---|
| `yai.tool` | Tool name |
| `yai.arguments` | Input arguments (JSON, truncated) |
| `yai.ok` | Success / failure |
| `yai.preview` | Truncated result preview on success (the kernel already caps it to 200 chars) |
| `yai.error` | Error message on failure; span Status is set to `ERROR` |
| `yai.without_call_event` | The result had no matching `tool_call` (composed tools / kernel meta-tools emit no call); an immediate span synthesized by the tracer |
| `yai.unclosed` | A tool span force-closed when the stream was cancelled / interrupted |

Tools execute **strictly serially** in the executor, and `tool_call` / `tool_result` never interleave, so a stack matched by tool name builds the parent/child relation correctly. Both missing-pair edges (a result without a call, a call without a result) are tolerated so no observation is lost.

## 4. Connecting Jaeger / Tempo (host-side)

The library never touches provider initialization. The host configures it once at process startup; afterwards `traced_run` picks up the global provider even without passing `tracer`.

### 4.1 OTLP/HTTP (recommended, language-agnostic)

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(
    BatchSpanProcessor(
        OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")
    )
)
trace.set_tracer_provider(provider)
```

Run Jaeger locally:

```bash
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest
# after a traced_run, open http://localhost:16686 and inspect yai.agent.run
```

### 4.2 OTLP/gRPC

Use `opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter` instead (the gRPC transport is included in the `[otel]` extra) with an endpoint like `http://localhost:4317` (no path). Grafana Tempo / any OpenTelemetry Collector works the same way — just point at the Collector.

> In production prefer `BatchSpanProcessor` (async, batched, low overhead); for tests/debugging use `SimpleSpanProcessor` with `ConsoleSpanExporter` to print spans to stdout.

## 5. Deliberate non-goals

- **No kernel intrusion**: tracing is just another consumer of the event stream; the kernel is unaware of it, so Agent behavior never depends on tracing being installed.
- **No hard dependency on opentelemetry**: `dependencies` in `pyproject.toml` stays empty; otel lives only in the `[otel]` extra.
- **No auto-configured reporting channel**: the library creates no provider and reads no env vars to reach a backend, avoiding "importing the library silently ships data out".
- **No final-reply body / memory content**: only lengths and structured fields are recorded, reducing the privacy surface of traces; if a host needs the body it adds its own span or `root_attributes`.
- **No change to ordering or pairing semantics**: spans open/close strictly in event-stream arrival order.

## 6. Offline tests

`tests/test_tracing_otel.py` uses the SDK's `InMemorySpanExporter` to assert the whole span tree in-process (root parent/child relations, ok/error status, the exception path, the orphan span for composed tools, interruption cleanup, attribute coercion) — **no collector and no API key**. Cases needing the SDK auto-skip when `[otel]` is absent; the "missing dependency yields install guidance" case always runs.
