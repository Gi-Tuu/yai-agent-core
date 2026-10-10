# 逐行讲解 27 · 运维三件套与 Docker 生产沙箱

> 对应源码：`spi/memory.py`、`memory/inmemory.py`、`memory/sqlite_store.py`、
> `batteries/fastapi_server/payload_guard.py`、`auth.py`、`shell/desktop/docker_sandbox.py`。
> 这些都是"上线给别人用"才需要的能力：临时数据会过期、返回体不会撑爆、接口有钥匙、
> 不可信代码关进容器。内核本身依旧零硬依赖。

## A. KV TTL：让临时键自动过期

### 1. 契约加一个可选参数（不破坏旧实现）

```python
async def put(self, key, value, ttl_seconds: int | None = None): ...
```

- `ttl_seconds=None`：永不过期，旧调用一行不用改。
- 传整数：到点后 `get` 返回 None。

### 2. 内存版：记一个"过期时刻"，惰性删除

```python
def put(self, key, value, ttl_seconds=None):
    ...
    self._kv_expires[key] = (now + ttl_seconds) if ttl_seconds else None

def get(self, key):
    exp = self._kv_expires.get(key)
    if exp is not None and now > exp:       # 严格大于：等于过期时刻仍保留
        self._kv.pop(key, None); self._kv_expires.pop(key, None)
        return None
    return self._kv.get(key)
```

- **惰性**：不跑后台线程扫，读的时候才判断（简单、零线程）。
- 边界口径：`now > exp` 才删，`==` 保留——与测试约定一致。

### 3. SQLite 版：schema v1 → v2 自动迁移

- kv 表加一列 `expires_at TEXT`；`PRAGMA user_version` 从 1 升到 2。
- 打开旧库（version==1）时执行迁移：`ALTER TABLE ... ADD COLUMN` + `user_version=2`，
  **旧数据原样保留**。
- `get` 的 SQL 加条件：`(expires_at IS NULL OR expires_at >= ?)`，边界时刻保留。
- 未来版本（如手动把 user_version 改成 3）打开即抛 `RuntimeError("schema 版本")`，
  防止内核误读"将来格式"。

## B. 响应体大小守卫：不把客户端撑爆

`payload_guard.py` 的纯函数 `bound_payload(payload, config)` 做三级截断：

1. **final_text 超长**：尾部截断加"…"。
2. **事件数超限**：`_head_tail` 保留开头 + 结尾，中间插一条占位事件，data 里记
   `omitted` 条数（不静默丢）。
3. **整体 JSON 仍超 max_total_chars**：逐级把保留事件数减半；降到 1 还超就只留占位。

输出附 `truncated: bool` 和 `events_total: 原始事件数`。原则：**宁可明确告诉你"我截了
多少"，也不偷偷删改**。`app.py` 在构造响应后、enabled 时调用它。

## C. API Key 鉴权：在花模型钱之前挡住

`auth.py` 的 `ApiKeyAuth`：

- 钥匙来源：`YAI_API_KEY`（单个）或 `YAI_API_KEYS`（逗号分隔多个），`from_env`
  去重保序；没配则 `enabled=False`（全公开，安全默认）。
- 保护范围：以 `/v1/` 开头的路径；`/health` 与验证文件等在 `PUBLIC_PATHS` 白名单内，
  永远公开（硬门槛要能匿名探活）。
- 校验：解析 `Authorization: Bearer <key>`（容忍裸 key）或 `X-API-Key`，用
  `hmac.compare_digest` 做**常量时间比较**（防计时侧信道）。

`app.py` 注册一个中间件：受保护路径没带 / 带错 key，**在进入 core、调用模型之前**
直接返回 401 + `WWW-Authenticate: Bearer`——不会因为别人乱刷而产生模型账单。

## D. Docker 生产沙箱：把不可信代码关进容器

教学级 `SubprocessSandbox` 只靠"子进程 + 白名单"，Windows 上还没有内存 / CPU 硬限。
`docker_sandbox.py` 的 `DockerSandbox` 实现同一 `ToolSandbox` 协议，内核零改动。

### 1. 复用 worker，不重复造

```python
_WORKER_SOURCE = (Path(__file__).parent / "_sandbox_worker.py").read_text(encoding="utf-8")
_ENTRY_SOURCE = _WORKER_SOURCE[_WORKER_SOURCE.index("from __future__"):]
```

- 剥到 `from __future__` 开头，才能作为 `python -c` 的入口（否则模块 docstring 在前，
  future 语句非法）。
- `-c` 下 `__name__ == "__main__"`，worker 末尾的 `main()` 会执行。

### 2. 容器 OS 级硬隔离（`_build_argv`）

| 开关 | 作用 |
|---|---|
| `--rm` | 退出即删，一次性 |
| `--network none` | 无网络命名空间，OS 级断网（不靠白名单） |
| `--read-only` + `--tmpfs /tmp:...:noexec` | 根 fs 只读，只给小的不可执行临时盘 |
| `--cap-drop ALL` | 丢弃所有 Linux capabilities |
| `--security-opt no-new-privileges` | 禁止提权 |
| `--memory` / `--cpus` | 内存 / CPU cgroup 上限，**跨平台**，补齐 Windows 短板 |
| `--pids-limit` | 限制进程数，防 fork 炸弹 |

payload（code/inputs）走容器 **stdin**、envelope 走 stdout，不挂载代码卷、不留临时文件；
`-c` 入口是**单个 argv**，不经 shell、无转义问题。

### 3. 超时与"Docker 不在"的处理

- 超时：`docker run` 客户端被杀不代表容器停了，按唯一 `--name` 执行 `docker rm -f` 兜底。
- 没装 / 没启动 Docker：`FileNotFoundError` 或 stderr 提示，返回可读错误，不抛穿内核。

### 4. 平台坑：真机用例只在 Linux 容器上跑

这套加固（`--read-only`、`--cap-drop`、`--tmpfs` 等）和镜像 `python:3.12-slim`
都是 **Linux 容器**特性。两种 Docker 环境要分清：

- **GitHub `ubuntu-latest`**：Docker 是 Linux 容器（OSType=linux）→ 真机端到端**真正执行**。
- **GitHub `windows-latest`**：Docker daemon 默认 **Windows 容器**（OSType=windows），
  不支持 `--read-only`，`docker run` 直接 `exit=125`（"read-only mode is not
  supported for Windows containers"）→ 真机用例必须 **skip**，而不是硬跑。
- **本地 Docker Desktop**：可在托盘右键切到 "Linux containers"，启动后 OSType=linux，
  真机即可跑；没启动则 skip。

gate 用 `docker info --format {{.OSType}}` 精确读取，不靠猜平台。

## E. 测试口径

- KV TTL：`FakeClock` 对两个 store 参数化对等 + v1→v2 迁移专项。
- 守卫 / 鉴权：纯函数 + `TestClient` 端到端（无 key 401、正确 key 200、硬门槛公开）。
- Docker：离线核对入口合法、argv 含全部加固 flags；真机 gate 用
  `docker info --format {{.OSType}}` 读取 daemon 容器类型，**只在 Linux 容器**
  （OSType=linux）执行，Windows 容器或无 daemon 自动 **skip**（不报错）；
  另用离线测试锁住 gate（windows→skip、linux→跑、不通→skip）。

## F. 自检

1. KV TTL 为什么用惰性删除而不是后台线程？"等于过期时刻"该保留还是删除？
2. 鉴权中间件为什么要放在"进入 core"之前？哪些路径必须保持公开？
3. 相比子进程沙箱，Docker 沙箱多了哪些 OS 级保证？为什么 Windows 特别需要它？
