# 部署、水平扩展与多租户指南

> 事实基线：yai-agent-core **v0.7.0**，最后核实 **2026-09-29**（本地仓库实测）。
> 单实例公网部署（Render / VPS + Caddy）见 [`docs/competitions/deployment.md`](competitions/deployment.md)；
> 本文回答更进一步的三个问题：**内核为什么能水平扩展、多租户怎么隔离、生产形态怎么搭。**

---

## 1. 先理解内核的实例模型（为什么能水平扩展）

`AgentCore` 是一个**普通对象、被动执行**，这是它能被随意复制扩展的根本原因：

- 它**不监听端口、不持有全局单例、不启动后台线程**；只有调用 `await core.run(task)` / `core.astream(task)` 时才工作。
- 每个实例自带**独立**的 `ToolRegistry`、`memory`、`policy`、`router`、`channel`、`executor`、`code_manager`。
- 会话状态全部存放在**构造时注入的 memory** 里，内核自身不持有可变业务状态 → **内核无状态、可多实例**。

```
                 ┌──────────────────────────────┐
   用户请求 ──▶  │  负载均衡 / 反向代理 (Caddy)  │
                 └──────────────┬───────────────┘
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
        core 实例 1        core 实例 2        core 实例 3     ← 无状态、可随意增减
              │                 │                 │
              └─────────────────┼─────────────────┘
                                ▼
              Memory SPI 后端（内存 / SQLite 文件 / 外部存储）
```

> 关键认知：**"多租户、高并发、分布式"主要是装配层与部署层的责任，不是内核要内置的功能。**
> 内核通过 SPI 契约把扩展点留给你，从而保持零硬依赖。

---

## 2. 三个隔离 / 扩展层级（按需选择，不要越级）

| 层级 | 形态 | 隔离手段 | 状态位置 | 适用场景 | 你要写的东西 |
|---|---|---|---|---|---|
| **L0** | 单租户 | 一个 core、`scope="default"` | 内存 / 单个 SQLite | 桌面应用、演示、单用户 | **现状即此**，无需改动 |
| **L1** | 进程内多租户 | 每租户独立 core，或同库不同 `scope` | 一个进程、多 scope | 单服务多用户、SaaS 雏形 | 一个"租户 → core"工厂（§5.1） |
| **L2** | 多实例多租户 | 多容器 + 负载均衡 + 分片 / 外部存储 | 每实例本地卷或共享存储 | 生产 SaaS、高并发 | 集群 compose + LB（§5.2 / §5.3） |

> 原则：**先用 L1 验证多租户逻辑，确有吞吐 / 可用性需求再上 L2。** 不要一上来就背 Redis / PG 全家桶。

---

## 3. 多租户隔离机制（内核已提供什么）

### 3.1 scope 隔离（`SqliteStore`）

`history` 与 `kv` 两张表都带 `scope` 列，所有读写按 `scope` 过滤，`clear()` 只清当前 scope：

- **一个 SQLite 文件可承载多个 scope**（多个租户 / 会话），逻辑隔离、互不影响；
- 但**一个 `SqliteStore` 实例在构造时固定一个 scope**——要服务多租户，就为每个 scope 建一个 store 实例（指向同一个 DB 文件，WAL 模式支持多连接并发）。

```python
from yai_core.memory import SqliteStore

# 同一个 DB 文件、不同 scope：每个租户拿到自己的 store
def store_for(tenant_id: str) -> SqliteStore:
    return SqliteStore("data/yai.db", scope=tenant_id)
```

### 3.2 实例隔离（每租户独立 core）

为每个租户创建独立 `AgentCore`，连 registry / policy / 工具集都彼此独立，隔离最彻底。
注意：`InMemoryStore` **没有 scope 参数（单空间）**，内存形态下多租户必须"每租户一个 store / core"。

### 3.3 怎么选

- 工具集相同、只需会话 / 记忆隔离 → **同库不同 scope**（最省资源）；
- 不同租户工具集 / 权限档位 / 沙箱不同 → **独立 core 实例**。

---

## 4. 水平扩展两条路线

### 路线 A：会话分片 / 粘性 —— 推荐，最契合嵌入式内核

按租户标识把每个租户**固定路由到一个实例**（一致性哈希），该租户的状态留在该实例（本地 SQLite 卷或内存），**不跨实例共享状态**：

- 优点：**无需分布式存储**，最贴合"零依赖、嵌入式"定位；
- 缺点：扩容时需要重新分片；实例故障只影响其名下租户（重路由到新实例，本地卷可重挂则历史保留，否则冷启动）。

### 路线 B：无状态化 + 共享外部存储

通过 **Memory SPI** 实现一个 PostgreSQL / Redis 版 `MemoryStore`，多实例共享，任意实例都能处理任意租户：

- 内核**不提供**分布式存储实现（契约已就绪），需要你写一个适配器（见 §5.3）；
- 优点：弹性扩容、故障切换干净；缺点：引入并维护外部存储。
- 注意：**流式（SSE）运行中的 agent loop 状态在该实例内存里，即使走路线 B，长连接仍需会话粘性**（见 §6）。

### 决策表

| 情况 | 推荐路线 |
|---|---|
| 嵌入式 / 边缘 / 中小规模、租户可绑定实例 | **A 分片 / 粘性** |
| 真正多租户 SaaS、需跨实例共享会话、大规模弹性 | **B 外部存储** |
| 暂时只是想让多个用户各聊各的 | **L1 进程内多租户**即可 |

---

## 5. 装配参考（文档示例，不进内核、不改源码）

> 以下是**装配层**写法。内核保持不变；你在自己的应用里组合它们。

### 5.1 进程内多租户 Core 工厂（L1）

```python
from yai_core import AgentCore
from yai_core.memory import SqliteStore

class CoreRegistry:
    """按租户缓存独立 core；同一 SQLite 文件、不同 scope 隔离记忆。"""

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

### 5.2 多实例 compose + Caddy 负载均衡（L2，路线 A）

现有 `docker-compose.yml` 把端口写死为 `127.0.0.1:8001`，`--scale` 会端口冲突。
集群形态用一个**不对宿主机映射端口、只在内网 expose** 的变体（示例 `docker-compose.cluster.yml`）：

```yaml
services:
  yai-agent-core:
    build: { context: . }
    image: yai-agent-core:latest
    expose: ["8000"]            # 仅内网，不对公网映射
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
  c-dy-data: {}
```

`deploy/Caddyfile.cluster`（Docker DNS 对 scale 出的多个实例返回多条记录，Caddy 自动展开并负载均衡）：

```
:80 {
    reverse_proxy yai-agent-core:8000 {
        lb_policy client_ip_hash   # 同一来源固定到同一实例（粗粒度粘性）
    }
}
```

启动 3 个实例：

```bash
docker compose -f docker-compose.cluster.yml up -d --build --scale yai-agent-core=3
```

> 说明：`client_ip_hash` 是 Caddy 确定支持的策略，可直接用；
> 若要严格"按租户 ID 而非来源 IP"分片，需在 LB 层按租户标识做一致性哈希，**具体指令请以当前 Caddy 版本的 `lb_policy` 文档为准并实测**，不要凭记忆写配置。

### 5.3 外部存储（路线 B）：实现 MemoryStore SPI

```python
import json
from yai_core.types import ChatMessage

class RedisMemoryStore:
    """实现 yai_core.spi.MemoryStore：历史与 KV 存 Redis，多实例共享。"""

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

> 你需要：`redis-py`，以及 `_msg_dict` / `_msg_from`（按 `ChatMessage` 的
> role / content / tool_calls / tool_call_id / name 字段做序列化）。
> 内核不提供这些——这正是 SPI 的意义：**存储后端由你决定，内核零依赖。**

---

## 6. 流式（SSE）与并发注意

- **请求-响应形态**（Battery 的 `POST /v1/agent/run`，一次性收完整 JSON）：天然无状态，任意实例可处理（只要 memory 可访问）。
- **流式形态**（各 host 网页的 SSE，如 host_e / host_g / host_i）：运行中的 agent loop 在**该实例内存**里，多实例部署必须做**会话粘性 / 绑定**，否则中途的事件流会断。
- **uvicorn worker 数**：模型调用是 **I/O 等待**，单 worker + asyncio 即可并发承载多个在途请求（I/O bound）；只有出现 CPU 瓶颈才考虑多 worker。
  - 多 worker 是多进程，`InMemoryStore` 跨进程**不共享**；需共享就用 SQLite（WAL）或外部存储。
- **反向代理对 SSE 的设置**：关闭响应缓冲、放大读超时。Caddy 对流式默认较友好；
  若用 Nginx，需 `proxy_buffering off;` 并调大 `proxy_read_timeout`，否则长连接被提前掐断。

---

## 7. 安全、配额与成本（多租户必看）

- **共享上游模型 Key**：现有 Battery 限流是"每 IP"（`YAI_RATE_LIMIT_*`）；多租户建议改为"**每租户**"限流 + 配额 / 预算，防止单个租户把共享 Key 的额度或并发耗尽。
- **每租户独立上游 Key**：隔离计费与配额的另一种方式，适合对成本敏感的部署。
- **沙箱隔离**：代码工具创建（`create_code_tool`）必须**每租户一个独立 `ToolSandbox`（SPI）**，不可共享；权限三档（全部审批 / 部分审批 / 无需审批）与白名单按租户独立配置。
- **工具可见性**：不同租户开放不同能力时，用独立 core / registry，从注册层保证"未授权工具对模型不可见"。
- **网络面**：容器端口只绑回环 / 内网，公网统一经反向代理进入（现有 compose 已如此）。

---

## 8. 落地清单（Checklist）

- [ ] 确认目标层级（多数情况 L1 已够）。
- [ ] L1：实现"租户 → core"工厂，用 scope 或独立实例隔离（§5.1）。
- [ ] 本地用两个租户交叉对话，验证记忆 / KV 互不可见、`clear()` 只影响本租户。
- [ ] 上 L2：决定路线 A（分片 / 粘性）或 B（外部存储 SPI）。
- [ ] 配置 LB：请求-响应可轮询；SSE 必须粘性。
- [ ] 把限流口径从"每 IP"改为"每租户"，配预算与告警。
- [ ] 确认每租户沙箱 / 权限独立；容器端口不直接对公网开放。
- [ ] 演练一次实例故障：租户能否重路由、历史是否保留。

---

## 9. 现状边界（诚实声明）

- `scripts/serve_example.py` 当前是**单租户演示装配**：模块加载时建一个全局 core、一个 `scope="default"` 的 store。**做多租户需要改装配层**；而内核所需能力（独立实例、scope、Memory SPI）均已具备。
- 内核**明确不内置**：Redis / PostgreSQL、服务发现、分布式锁、负载均衡、多租户 Key 管理——这些由部署层 / SPI 负责，以守护零硬依赖红线。
- **SQLite 是文件级数据库，适合单机 / 嵌入式，不适合跨主机直接共享**；跨主机水平扩展请走"外部存储 SPI（路线 B）"或"会话分片（路线 A）"。

---

## 附：相关文件（仓库内）

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 在线 API 镜像，`uvicorn scripts.serve_example:app`，监听 `${PORT:-8000}`，含 HEALTHCHECK |
| `docker-compose.yml` | 单实例，端口绑 `127.0.0.1:8001:8000` |
| `docker-compose.deploy.yml` | 叠加 Caddy（80/443，自动 HTTPS） |
| `render.yaml` | Render Blueprint（免费演练，会休眠） |
| `deploy/Caddyfile` | 单实例反代示例 |
| `src/yai_core/spi/memory.py` | `MemoryStore` 契约（历史 + KV） |
| `src/yai_core/memory/sqlite_store.py` | 带 scope 隔离的持久化实现（WAL） |
| `src/yai_core/memory/inmemory.py` | 进程内单空间实现 |
| `src/yai_core/batteries/fastapi_server/` | FastAPI Battery + 限流 |
