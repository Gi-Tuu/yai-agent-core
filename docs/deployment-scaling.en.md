# Deployment, Horizontal Scaling, and Multi-Tenancy Guide

> 中文：[deployment-scaling.md](deployment-scaling.md)

> Baseline of fact: yai-agent-core **v0.9.0**, last verified **2026-10-12** (against the local repository).
> Single-instance public deployment (Render / VPS + Caddy) is covered in
> [`docs/competitions/deployment.md`](competitions/deployment.md); this document answers three questions
> one level further: **why the kernel scales horizontally, how tenants are isolated, and how to build the
> production shape.**
>
> **Stance in one sentence:** the kernel is always an in-process, single-tenant, SQLite-style kernel; a
> **`tenant_id` never appears in any kernel method signature.** Multi-tenancy is the job of the host /
> Battery at the **assembly layer**, which builds and holds a set of mutually non-shared SPI instances per
> tenant. When centralized scale is needed, swap the backend to a client/server database through the
> Storage / Memory SPI — do not turn the kernel into a server.

---

## 1. First understand the instance model (why it scales horizontally)

`AgentCore` is an **ordinary, passive object** — that is the root reason it can be freely replicated:

- It **does not listen on a port, hold a global singleton, or start background threads**; it only works when you call `await core.run(task)` / `core.astream(task)`.
- Each instance carries its **own independent** `ToolRegistry`, `memory`, `policy`, `router`, `channel`, `executor`, and `code_manager`.
- Session state lives entirely in the **memory injected at construction**; the kernel itself holds no mutable business state → **the kernel is stateless and multi-instance capable**.
- **Tasks are serialized within one instance**: `astream`/`run` hold an instance-level `asyncio.Lock` for the whole call, so concurrent requests hitting the same core queue up rather than interleaving at `await` points and corrupting shared context / memory / route-learning state. To carry multiple independent sessions concurrently, assemble **multiple instances** per L1 / L2 below instead of piling concurrency onto a single core; stateless model / discovery sources / sandboxes may be shared across instances. The full concurrency contract is in [`architecture.en.md` §2.6](architecture.en.md#26-concurrency-model-and-multi-tenant-isolation).

```
                 ┌──────────────────────────────┐
   user request ─▶  load balancer / reverse proxy (Caddy)
                 └──────────────┬───────────────┘
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
        core instance 1   core instance 2   core instance 3   ← stateless, scale freely
              │                 │                 │
              └─────────────────┼─────────────────┘
                                ▼
              Memory SPI backend (in-memory / SQLite file / external store)
```

> Key insight: **"multi-tenancy, high concurrency, distributed" is primarily the responsibility of the
> assembly and deployment layers, not a feature the kernel must build in.** The kernel leaves the extension
> points to you through SPI contracts, which keeps it zero-hard-dependency.

---

## 2. Three isolation / scaling levels (choose on demand, do not skip ahead)

| Level | Shape | Isolation | State location | Use case | What you write |
|---|---|---|---|---|---|
| **L0** | Single tenant | one core, `scope="default"` | in-memory / single SQLite | desktop app, demo, single user | **this is the current default**, no change |
| **L1** | In-process multi-tenant | a core per tenant, or multiple scopes in one DB | one process, multiple scopes | single service serving many users, SaaS prototype | a "tenant → core" factory (§5.1) |
| **L2** | Multi-instance multi-tenant | multiple containers + LB + sharding / external store | per-instance local volume or shared store | production SaaS, high concurrency | cluster compose + LB (§5.2 / §5.3) |

> Principle: **validate multi-tenant logic at L1 first; move to L2 only when throughput / availability
> demands it.** Do not adopt the whole Redis / PG stack up front.

---

## 3. Multi-tenant isolation (what the kernel already provides)

### 3.1 scope isolation (`SqliteStore`) — the intra-tenant specialist / session boundary

Both the `history` and `kv` tables carry a `scope` column; all reads and writes filter by `scope`, and
`clear()` only clears the current scope:

- **One SQLite file can host multiple scopes**, logically isolated from one another;
- but **a `SqliteStore` instance fixes one scope at construction** — to isolate multiple sessions, build one store instance per scope (pointing at the same DB file; WAL mode allows concurrent connections).

```python
from yai_core.memory import SqliteStore

# Within one trusted principal (one org / one user), use scope to separate specialists / sessions:
def store_for(tenant_db: str, specialist: str) -> SqliteStore:
    return SqliteStore(tenant_db, scope=specialist)
```

> ⚠️ **scope is an *intra-tenant* boundary, not a *cross-organization* hard boundary.** Do not isolate
> mutually untrusted enterprise tenants with "one SQLite file + a scope per company": that is only a
> `WHERE scope=?` logical filter — one missed query leaks across organizations, and a single file can only
> carry one data encryption key (DEK), so you cannot have per-organization keys / backups / exports /
> delete-on-offboarding. **For hard isolation across untrusted tenants see §3.4 (SQLite-per-tenant + a DEK
> per tenant, or Postgres RLS).**

### 3.2 Instance isolation (a separate core per tenant)

Create an independent `AgentCore` per tenant so that even the registry / policy / toolset are separate —
the strongest isolation. Note that `InMemoryStore` **has no scope parameter (single namespace)**; in the
in-memory form, multi-tenancy requires "one store / core per tenant".

### 3.3 How to choose

First distinguish "**multiple sessions within one trusted principal**" from "**multiple mutually untrusted
companies / organizations**":

- Within one org / user, only separating specialists / sessions, with identical toolsets and keys → **multiple scopes in one DB** (cheapest);
- Same trust domain but different toolsets / permission levels / sandboxes → **separate core instances** (may share one DB file);
- **Across companies / untrusted organizations** (e.g. one backend serving firms A and B) → **a separate storage unit + a separate DEK per tenant**, see §3.4; do not use same-DB scopes as the hard boundary.

### 3.4 Hard isolation across untrusted tenants: SQLite-per-tenant + a DEK per tenant

The most worry-free hard-isolation shape for a small/medium server: **one directory, one SQLite file, one
vault, one data key per tenant.** The kernel stays unaware of tenants; everything happens in the assembly
factory:

```
data/tenants/
├── tenant-a/
│   ├── state.db        # this tenant's history / KV (scope then splits specialists / sessions inside)
│   ├── vault.json      # this tenant's long-term memory (encrypted at rest with this tenant's DEK)
│   └── (the DEK is never stored in plaintext; it is held under KMS envelope encryption)
└── tenant-b/ ...        # files and keys fully independent from tenant-a
```

```python
import os
from yai_core import AgentCore
from yai_core.memory import SqliteStore
from yai_core.memory.vault import MemoryVault
# StorageCipher is the 9th SPI; the [crypto] extra ships FernetStorageCipher
# (generate_key() / from_env("YAI_VAULT_KEY")). Use one DEK per tenant in multi-tenant deployments.

class TenantCoreFactory:
    """Independent store / vault / cipher / policy / budget per tenant; idle cores can be LRU-evicted."""

    def __init__(self, host, model, kms_client, root="data/tenants"):
        self._host, self._model, self._kms, self._root = host, model, kms_client, root
        self._live: dict[str, AgentCore] = {}

    def get(self, tenant: str) -> AgentCore:
        if tenant not in self._live:
            d = os.path.join(self._root, tenant)
            os.makedirs(d, exist_ok=True)
            dek = self._kms.unwrap(tenant)          # KMS envelope-unwraps this tenant's own DEK
            cipher = make_cipher(dek)               # implements StorageCipher (e.g. Fernet)
            memory = SqliteStore(os.path.join(d, "state.db"))   # separate file = hard boundary
            vault = MemoryVault(os.path.join(d, "vault.json"), cipher=cipher)
            self._live[tenant] = AgentCore(
                self._model, channel=self._host, memory=memory,
                long_memory=vault, policy=policy_for(tenant),    # per-tenant allowlist / approval level
            )
        return self._live[tenant]

    def evict_idle(self, max_live: int) -> None:
        """Idle eviction: stop surplus cores and release connections; files persist; get() cold-rebuilds."""
```

Key points:

- **One DEK per tenant**; the key-encryption key (KEK) lives in a KMS doing envelope encryption and is
  never shared across tenants. Offboarding a tenant = delete its directory and have the KMS destroy its
  DEK, yielding a clean **data-residency / export / deletion unit**.
- **vault recall and the proactive memory injection at task start are strictly scoped to that tenant's
  vault** — never do a global recall that crosses tenants.
- At large scale / when cross-instance sharing is needed, upgrade "a file per tenant" to **PostgreSQL +
  Row-Level Security (RLS)**: implement a Memory / Storage SPI adapter that enforces RLS on the connection
  via per-tenant credentials or `SET app.tenant`; rate limiting / metering can move to Redis. **The kernel
  does not ship these backends** (the contracts are ready), see §5.3.
- The sub-employee concurrency gate `DelegationSupervisor` semaphore is "global within one root" by
  default; when multiple tenants share a process it should be **a separate supervisor per tenant**,
  otherwise tenant A exhausting the concurrency slots starves tenant B. Only share one explicitly if you
  want to cap whole-machine concurrency.

### 3.5 Per-tenant isolation matrix (implemented item by item at the assembly layer)

| Dimension | Multi-tenant approach | Contract / carrier |
|---|---|---|
| History / context | a separate `.db` file per tenant (hard boundary); Postgres + RLS at scale | Memory / Storage SPI |
| Long-term memory vault | a separate vault per tenant; **recall strictly limited to that tenant** | `MemoryVault` |
| Data keys | one DEK per tenant, KEK in KMS envelope encryption, never shared | `StorageCipher` SPI (9th contract, `[crypto]`) |
| Permission levels | a per-tenant tool allowlist / approval level (all / partial / no approval) | Policy SPI |
| Rate limit / budget / metering | limiter key, Budget, and metrics labels all keyed by tenant | Battery (assembly layer) |
| Model | inject each tenant's own key for separate billing, or meter a platform key per tenant | ModelProvider SPI |
| Code sandbox | a separate container per task / tenant with network isolation, never shared | ToolSandbox SPI |
| Tool / MCP catalog | different visible toolset per tenant; unauthorized tools invisible to the model | Discovery SPI / separate registry |
| Identity (user/org → tenant) | **the host IdP owns this**; the Battery only keeps an auth hook; YAI builds no identity system | host |

> **Prompt-cache tenant boundary:** the stable system prefix (containing no tenant data; see the
> deterministic catalog ordering in walkthrough 41) may be shared across tenants for cache hits to save
> cost; but history, long-term memory, and tool results stay strictly in tenant partitions — **never splice
> two tenants' content into one cached prefix or one model call.**

---

## 4. Two horizontal-scaling routes

### Route A: session sharding / stickiness — recommended, best fit for an embedded kernel

Pin each tenant to one instance by a tenant identifier (consistent hashing); that tenant's state stays on
that instance (local SQLite volume or in-memory), **no cross-instance state sharing**:

- Pros: **no distributed storage needed**, closest to the "zero-dependency, embedded" position;
- Cons: resharding is needed on scale-out; an instance failure affects only its tenants (reroute to a new instance — history is retained if the local volume can be reattached, otherwise cold start).

### Route B: statelessness + shared external storage

Implement a PostgreSQL / Redis `MemoryStore` through the **Memory SPI** so multiple instances share state
and any instance can serve any tenant:

- The kernel **does not provide** a distributed-storage implementation (the contract is ready); you write an adapter (see §5.3);
- Pros: elastic scaling, clean failover; cons: you introduce and maintain external storage.
- Note: **during a streaming (SSE) run the agent-loop state lives in that instance's memory** — even with route B, long connections still need session stickiness (see §6).

### Decision table

| Situation | Recommended route |
|---|---|
| embedded / edge / small-medium scale, tenants can be bound to an instance | **A sharding / stickiness** |
| true multi-tenant SaaS, cross-instance shared sessions, large elastic scale | **B external storage** |
| you just want several users to chat independently | **L1 in-process multi-tenant** is enough |

---

## 5. Assembly references (documentation examples; not in the kernel, no source changes)

> The following are **assembly-layer** patterns. The kernel stays unchanged; you compose them in your own
> application.

### 5.1 In-process multi-tenant Core factory (L1, within one trust domain)

> For separating specialists / sessions by scope within one org / user; **for hard isolation across
> untrusted companies (separate files + separate DEKs + idle LRU eviction) use the `TenantCoreFactory` in
> §3.4**, not the "multiple scopes in one DB" shown here.

```python
from yai_core import AgentCore
from yai_core.memory import SqliteStore

class CoreRegistry:
    """Cache an independent core per tenant; isolate memory by scope in one SQLite file."""

    def __init__(self, host, model, db_path: str = "data/yai.db"):
        self._host, self._model, self._db = host, model, db_path
        self._cores: dict[str, AgentCore] = {}

    def get(self, tenant_id: str) -> AgentCore:
        if tenant_id not in self._cores:
            memory = SqliteStore(self._db, scope=tenant_id)
            self._cores[tenant_id] = AgentCore.auto(
                self._host, self._model, memory=memory
            )
        return self._cores[tenant_id]
```

### 5.2 Multi-instance compose + Caddy load balancing (L2, route A)

The current `docker-compose.yml` hard-codes the port to `127.0.0.1:8001`, which conflicts under `--scale`.
For a cluster shape use a variant that **does not publish a host port and only exposes on the internal
network** (example `docker-compose.cluster.yml`):

```yaml
services:
  yai-agent-core:
    build: { context: . }
    image: yai-agent-core:latest
    expose: ["8000"]            # internal only, no host publishing
    env_file: [.env]
    restart: unless-stopped
  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes:
      - ./deploy/Caddyfile.cluster:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
    depends_on: [yai-agent-core]
volumes:
  caddy-data: {}
```

`deploy/Caddyfile.cluster` (Docker DNS returns multiple records for scaled instances; Caddy expands them
and load-balances automatically):

```
:80 {
    reverse_proxy yai-agent-core:8000 {
        lb_policy client_ip_hash   # pin the same source to the same instance (coarse stickiness)
    }
}
```

Start 3 instances:

```bash
docker compose -f docker-compose.cluster.yml up -d --build --scale yai-agent-core=3
```

> Note: `client_ip_hash` is a policy Caddy supports deterministically and can be used directly; to shard
> strictly by tenant ID rather than source IP, do consistent hashing on the tenant identifier at the LB
> layer — **follow the current Caddy `lb_policy` documentation and test it**, do not rely on memorized
> config.

### 5.3 External storage (route B): implement the MemoryStore SPI

```python
import json
from yai_core.types import ChatMessage

class RedisMemoryStore:
    """Implements yai_core.spi.MemoryStore: history and KV in Redis, shared across instances."""

    def __init__(self, redis, *, scope: str = "default"):
        self._r, self._scope = redis, scope

    async def append_history(self, message: ChatMessage) -> None:
        self._r.rpush(f"hist:{self._scope}", json.dumps(_msg_dict(message)))

    def history(self) -> list[ChatMessage]:
        return [_msg_from(d) for d in self._r.lrange(f"hist:{self._scope}", 0, -1)]

    async def put(self, key: str, value: str) -> None:
        self._r.hset(f"kv:{self._scope}", key, value)

    async def get(self, key: str) -> str:
        return self._r.hget(f"kv:{self._scope}", key)

    async def clear(self) -> None:
        self._r.delete(f"hist:{self._scope}", f"kv:{self._scope}")
```

> You need `redis-py`, plus `_msg_dict` / `_msg_from` (serialize by the `ChatMessage`
> role / content / tool_calls / tool_call_id / name fields). The kernel does not provide these — that is
> the point of the SPI: **you choose the storage backend, the kernel stays dependency-free.** For
> cross-organization hard isolation prefer PostgreSQL + RLS over a shared key prefix.

---

## 6. Streaming (SSE) and concurrency notes

- **Request/response** (the Battery's `POST /v1/agent/run`, one-shot full JSON): naturally stateless, any instance can handle it (as long as memory is reachable).
- **Streaming** (SSE in the host web apps, e.g. host_e / host_g / host_i): the running agent loop is **in that instance's memory**; multi-instance deployment must use **session stickiness / pinning**, otherwise the event stream breaks mid-run.
- **uvicorn worker count**: model calls are **I/O waits**, so a single worker + asyncio can concurrently carry many in-flight requests (I/O bound); only add workers when you hit a CPU bottleneck.
  Multiple workers are multiple processes, and `InMemoryStore` is **not shared** across processes; use SQLite (WAL) or external storage to share.
- **Reverse-proxy settings for SSE**: disable response buffering and raise read timeouts. Caddy is fairly friendly to streaming by default; with Nginx you need `proxy_buffering off;` and a larger `proxy_read_timeout`, or long connections are cut early.

---

## 7. Health checks, readiness probes, and metrics scraping (/health, /readyz, /metrics)

The Battery provides three GET observability endpoints; keep **liveness** and **readiness** separate in
your deployment:

| Endpoint | Meaning | Orchestration action on failure | Checks downstream? | Auth |
|---|---|---|---|---|
| `GET /health` | **liveness**: is the process alive | **restart / recreate** | No (returns commit only) | always public |
| `GET /readyz` | **readiness**: can it take traffic now | **pull from LB (no restart)** | Yes (probes concurrent, 5s timeout, individually isolated) | always public |
| `GET /metrics` | Prometheus metrics | scrape only, no traffic effect | No | protected when a Key is set; `YAI_METRICS_PUBLIC=1` exposes it |

**Why liveness must not check the database / model**: if a downstream blip fails liveness, the orchestrator
keeps restarting the pod while startup itself depends on downstream recovery — a restart storm from which
rebooting cannot recover. Downstream health belongs only in readiness — failure just removes traffic, and
when the downstream recovers the probe turns ok and traffic is automatically restored.

### 7.1 Register custom readiness probes

The default probes only report local state (number of registered tools, memory backend type); they **do
not call the model or make network requests**. Real downstream checks are registered at the assembly
layer:

```python
async def db_probe():
    ok = await ping_database()          # the host's own connection
    return True if ok else "database unreachable"   # a string = failure reason

app = create_app(
    core,
    readiness_probes={"database": db_probe},
    readiness_timeout=5.0,
)
```

A probe may be sync or async; returning `None/True` means ready, `False/str` means not ready, a `dict`
uses its `ok` key, and raising means failure. All probes run concurrently; a single probe timing out or
raising only affects itself (that probe is `status=error`, the endpoint as a whole returns 503) and never
takes down the endpoint.

### 7.2 What to fill in per platform

- **Kubernetes**:

  ```yaml
  livenessProbe:
    httpGet: { path: /health, port: 8000 }
  readinessProbe:
    httpGet: { path: /readyz, port: 8000 }
  ```

- **Render / Caddy / Nginx / ordinary LBs**: use `/readyz` as the health-check URL (it decides traffic); do not use `/v1/agent/run`.
- **Caddy / Nginx proxying SSE**: still disable buffering and raise timeouts per §6; the probe path must not go through auth.

### 7.3 Prometheus scraping /metrics

- By default it returns Prometheus text exposition (`yai_agent_runs_total{outcome}`,
  `yai_agent_tool_calls_total{tool,result}`, `yai_agent_model_calls_total{tier}`,
  `yai_run_duration_seconds_bucket/...`, etc.); add `?format=json` for a structured snapshot for humans / debugging.
- **With no API Key**: `/metrics` is scrapable directly.
- **With `YAI_API_KEY` set**: `/metrics` returns 401 by default. Two ways to allow it:
  - the scraper sends a Bearer: in the Prometheus scrape_config set `authorization: { credentials: "Bearer <key>" }` (or use `bearer_token_file`);
  - expose explicitly on the internal network: set `YAI_METRICS_PUBLIC=1`, which makes only `/metrics` anonymously scrapable while `/v1/**` business paths still require a Key.
- Probe and metrics endpoints **are not subject to the rate-limit middleware** (it only guards `POST /v1/agent/run`), so high-frequency scraping never triggers 429.

### 7.4 Metrics semantics across multiple instances

Each process's `/metrics` counts only **that process** (per-process counters); the kernel does no
cross-process aggregation (that would reintroduce a centralized dependency against the embedded position).
When scaling out, have Prometheus label and scrape each instance separately and aggregate at query time:

```promql
sum by (outcome) (increase(yai_agent_runs_total[5m]))        # cluster-wide run outcomes
sum by (tier)     (increase(yai_agent_model_calls_total[1d]))  # proxy for strong-tier model spend
```

To emit OpenTelemetry / OTLP instead of scraping text, use the optional `[otel]` extra's
`integrations/metrics.register_otel_meter` (observable instruments, pull at collection time, no Provider
initialized in the library); see walkthrough 38.

---

## 8. Security, quotas, and cost (required reading for multi-tenancy)

- **Shared upstream model Key**: the current Battery limiter is "per IP" (`YAI_RATE_LIMIT_*`); for multi-tenancy change it to **per-tenant** limiting + quotas / budgets so one tenant cannot exhaust the shared Key's quota or concurrency.
- **Per-tenant upstream Key**: another way to isolate billing and quotas, suited to cost-sensitive deployments.
- **Sandbox isolation**: code-tool creation (`create_code_tool`) must use **a separate `ToolSandbox` (SPI) per tenant**, never shared; the three permission levels (all approval / partial approval / no approval) and the allowlist are configured independently per tenant.
- **Tool visibility**: when different tenants get different capabilities, use separate cores / registries so "unauthorized tools are invisible to the model" at the registration layer.
- **Encryption at rest and keys**: for cross-organization tenants, configure an independent vault file + independent DEK per tenant (KEK under KMS envelope encryption), see §3.4 / §3.5; with no cipher the vault is plaintext JSON and is only suitable for a single end-side user or self-encrypted disks.
- **Long-term memory must not cross tenants**: vault recall and proactive task-start memory injection must be constrained to that tenant's store / vault; the sub-employee concurrency semaphore is independent per tenant.
- **Prompt-cache boundary**: the tenant-data-free stable system prefix may be shared across tenants; history / memory / tool results are strictly partitioned and never spliced across tenants (see §3.5).
- **Network surface**: container ports bind only to loopback / the internal network; public traffic enters uniformly through the reverse proxy (the current compose already does this).

---

## 9. Landing checklist

- [ ] Confirm the target level (L1 is enough in most cases).
- [ ] L1: implement a "tenant → core" factory, isolating by scope or by independent instance (§5.1).
- [ ] Cross-organization: use SQLite-per-tenant + a per-tenant DEK (§3.4), not same-DB scopes.
- [ ] Locally interleave conversations across two tenants and verify memory / KV are mutually invisible and `clear()` affects only that tenant.
- [ ] For L2: decide route A (sharding / stickiness) or B (external storage SPI).
- [ ] Configure the LB: request/response may be round-robined; SSE must be sticky.
- [ ] Put `/health` in liveness and `/readyz` in readiness; never check downstream in liveness (§7).
- [ ] Register readiness probes for real dependencies such as the database / model, and rehearse a dependency failure: traffic removed, no restart.
- [ ] Configure Prometheus to scrape `/metrics` (Bearer when a Key is set, or `YAI_METRICS_PUBLIC=1`) with multi-instance aggregation and alerts.
- [ ] Change the limiter dimension from "per IP" to "per tenant" and set budgets and alerts.
- [ ] Confirm per-tenant sandbox / permission independence; container ports are not exposed directly to the public internet.
- [ ] Rehearse an instance failure: can tenants be rerouted, is history retained.

---

## 10. Current boundary (honest statement)

- `scripts/serve_example.py` is currently a **single-tenant demo assembly**: at module load it builds one global core and one store with `scope="default"`. **Multi-tenancy requires changing the assembly layer**; the capabilities the kernel needs (independent instances, scopes, the Memory SPI, the cipher SPI) are all already present.
- The kernel **deliberately does not bundle**: Redis / PostgreSQL, service discovery, distributed locks, load balancing, or multi-tenant key management — these belong to the deployment layer / SPI to protect the zero-hard-dependency line.
- **SQLite is a file-level database suited to a single machine / embedded use; it is not meant to be shared directly across hosts.** For cross-host horizontal scaling use the "external storage SPI (route B)" or "session sharding (route A)".
- A per-tenant `core_factory`, idle LRU eviction, a Postgres+RLS Storage adapter, and Redis-based limiting are **documented assembly patterns / roadmap items**, not built-in kernel APIs.

---

## Appendix: relevant files (in the repo)

| File | Role |
|---|---|
| `Dockerfile` | online API image, `uvicorn scripts.serve_example:app`, listens on `${PORT:-8000}`, with HEALTHCHECK |
| `docker-compose.yml` | single instance, port bound to `127.0.0.1:8001:8000` |
| `docker-compose.deploy.yml` | overlay adding Caddy (80/443, automatic HTTPS) |
| `render.yaml` | Render Blueprint (free-tier rehearsal, sleeps when idle) |
| `deploy/Caddyfile` | single-instance reverse-proxy example |
| `src/yai_core/spi/memory.py` | the `MemoryStore` contract (history + KV) |
| `src/yai_core/spi/cipher.py` | the `StorageCipher` contract (encrypt/decrypt bytes, 9th SPI) |
| `src/yai_core/memory/sqlite_store.py` | persistent implementation with scope isolation (WAL) |
| `src/yai_core/memory/vault.py` | long-term memory vault; optional `cipher=` for encryption at rest |
| `src/yai_core/memory/inmemory.py` | in-process single-namespace implementation |
| `src/yai_core/batteries/fastapi_server/` | FastAPI Battery + rate limiting + auth |
| `src/yai_core/batteries/fastapi_server/readiness.py` | `/readyz` readiness probes (concurrent / timeout / exception isolation) |
| `src/yai_core/metrics.py` | zero-dependency metrics collector + `/metrics` Prometheus text |
| `src/yai_core/integrations/metrics/` | OTel meter bridge (optional `[otel]` extra) |
