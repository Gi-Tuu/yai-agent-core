# 逐行讲解 14 · 限流 Battery：评审期保护公网 POST 端点

> 对应代码：`batteries/fastapi_server/ratelimit.py`（新增）、`app.py`（中间件挂载）。
> v0.3，纯标准库（collections/threading/math），零新依赖；默认开启是对外承诺值。

## A. 为什么限流、限哪里

线上 `/v1/agent/run` 是真金白银的端点：每次 POST 都会调一次 DeepSeek。评审期最怕
两类事故——失控脚本死循环重试把额度烧光、爬虫误扫打爆免费实例。限流 Battery 的
答案很克制：

- **只拦 `POST /v1/agent/run`**，白名单写死在 `_LIMITED_PATHS`；
- **三个 GET 端点（/health、/.well-known/xagent-verification.json、/v1/tools）永不限**：
  它们是 X-Agent 评审的硬门槛验证路径，任何"顺手全局限一下"都可能让评审官看到 429——
  这是本批的第一设计红线；
- 中间件 + 路径白名单（而不是逐路由 `Depends`）：以后新增 POST 路由时，
  "忘记挂限流"比"忘记加 Depends"更不容易发生——白名单是默认在场的一道闸。

默认值 `enabled=True / per_minute=30 / window_seconds=60` 是对外承诺：
每 IP 每分钟 30 次 POST，正常评审用不完，失控脚本立刻被拦。

## B. 滑动窗口日志：最好解释的算法

```python
hits: dict[str, deque[float]]   # 每 key 一个时间戳队列

def check(key):
    now = time_func()
    while hits[key] and hits[key][0] <= now - window:   # 1. 弹出窗口外时间戳
        hits[key].popleft()
    if len(hits[key]) >= per_minute:                    # 2. 剩余长度已达上限 → 拒
        retry_after = max(1, ceil(oldest + window - now))
        return False, retry_after
    hits[key].append(now)                               # 3. 记录本次并放行
    return True, 0
```

对比固定窗口（"每分钟清零计数器"）：固定窗口在边界处有 2× 突发（59 秒 30 次 +
61 秒 30 次 = 2 秒 60 次）；滑动窗口日志语义就是字面的"任意 60 秒内最多 30 次"，
没有边界突发，也最好向评审解释。代价是 O(窗口内请求数) 内存——单实例演示流量下
可以忽略。

`Retry-After = max(1, ceil(oldest + window - now))`：队首（最老）请求过期的那一刻
就是配额腾出空位的那一刻，向上取整保证至少等 1 秒。**响应头与响应体里的
`retry_after` 来自同一次计算**——不一致会让客户端重试策略无所适从。

## C. client_ip：信任边界要写清楚

```python
X-Forwarded-For 最左非空段 → 缺失回退 request.client.host → 再缺省 "unknown"
```

Render 边缘代理会为每个请求附加 XFF，所以取 XFF 最左即真实客户端 IP；
不依赖 uvicorn `--proxy-headers`，部署形态少一个变量。

**威胁模型如实写**：这套识别防的是误刷/爬虫/失控循环；**不防**蓄意伪造 XFF 的
分布式攻击——攻击者可以随便声明 `X-Forwarded-For: 8.8.8.8` 换桶。那要靠鉴权或
人机验证，超出 v0.3 范围。文档里不装强，评审官反而信得过。

## D. 并发与生命周期：两处"防御性多虑"

- `threading.Lock` 包住 `check()`：目标路由是 async，deque 操作中间没有 await，
  单事件循环下理论原子；但 TestClient 会跨线程、将来同步路由也可能跨线程，
  锁成本近乎为零，买了保险。
- 状态只在内存：进程重启清零，不落盘、不引 Redis。演示服务重启后限流从零开始，
  这在评审语境下是可接受的边界（攻击者拿到的也只是重新数 30 次）。

## E. 装配：create_app 只加一个关键字参数

```python
def create_app(core, lifespan=None, *, rate_limit: RateLimitConfig | None = None):
    config = rate_limit if rate_limit is not None else RateLimitConfig.from_env()
    if config.enabled:
        ...注册中间件...
```

- `None` → `from_env()`：既有调用（`create_app(core)`、`create_app(core, lifespan=...)`）
  零改动，行为即"默认开启"；
- `enabled=False` 时**根本不注册中间件**：关闭态零开销、零行为变化，
  本地开发设 `YAI_RATE_LIMIT_ENABLED=0` 即回到改造前；
- 被限请求在中间件层直接返回 429，**不进 `core.run`**——一分模型钱都不花
  （测试用计数模型断言：N+1 次请求只调 N 次模型）。

## F. 测试：假时钟贯穿，禁止 sleep

窗口是 60 秒，真实等待不可能出现在测试里。全部用 `time_func` 注入假时钟：
`now = [0.0]`，`clock.advance(seconds=61)` 一行"等"过一分钟。能注入的前提是
`check()` 只通过 `time_func()` 取时间——**永远不直接调 time.monotonic**，
这是可测性的代价与回报。边界语义钉死在用例里：恰好等于窗口的时间戳按过期清除
（`<=`），`retry_after` 恒 ≥ 1。

## G. 全局兜底配额：每 IP 各留一点，也能把实例刷爆

每 IP 维度有个天然盲区：来一万个赞不同的 IP，每个只发三次，`per_minute=30` 一次都不会触发，
但 `/v1/agent/run` 是真金白银——三万次 DeepSeek 调用足够把免费层烧穿。所以加第二个维度：
**跨所有来源的总配额** `global_per_minute`，默认 `0`（关闭，行为与旧版逐字节一致），
`YAI_RATE_LIMIT_GLOBAL_PER_MINUTE=120` 之类才生效。

实现上刻意**不写第二套算法**：全局闸就是同一个 `SlidingWindowLimiter`，只是配额换一下、
key 固定成 `"__global__"`：

```python
limiter = SlidingWindowLimiter(config)                       # 每 IP
global_limiter = (
    SlidingWindowLimiter(config, per_minute=config.global_per_minute)
    if config.global_per_minute > 0
    else None
)                                                            # 全局（同一个类）
```

两套窗口逻辑必须一模一样，否则"全局"和"每 IP"会长出两种限流语义（恢复时机、
`Retry-After` 口径都不同），那种分叉最难查。

**先算全局、再算每 IP**：全局拒绝时不消耗该 IP 的配额；反过来"全局放行但该 IP 超限"
会白扣一个全局名额——宁可多拦不可漏放，且只在总配额真的快耗尽时才发生。
响应体多带一个 `scope: "global" | "ip"`，运维看日志一眼知道是哪道闸命中的。

红线没变：全局闸**仍然只挂在 `POST /v1/agent/run` 白名单里**。三个 GET 端点是评审硬门槛
路径，"顺手把全局也限上"正好会让评审官看到 429——这条有测试钉住
（`test_get_endpoints_never_hit_global_gate`）。

## H. 诚实的边界

1. 不防伪造 XFF 的分布式攻击（见 C 节）；
2. 重启清零，无跨实例共享计数——**`global_per_minute` 只是本进程内的总数**，
   多实例部署时每个实例各算各的，它不是分布式限流；要做跨实例总闸得先解决状态共享
   （Redis / 一致性计数），那属于另一层工程；
3. 全局限流兜底已实现（G 节），但默认关闭：开不开是宿主的容量决策，不由库替它定；
4. `X-Forwarded-For` 链上只有最左段被信任，右段可能是任意代理追加的，不做校验。

## 自检

1. 为什么三个 GET 端点永远不限流？加"软限"会怎样？
2. 滑动窗口日志与固定窗口计数差在哪？本批为什么选前者？
3. `Retry-After` 怎么算的？为什么和响应体必须同源？
4. 伪造 XFF 能绕过本限流吗？那属于哪一层的问题？
5. 为什么 `enabled=False` 时不注册中间件，而不是注册了直接放行？
6. 被限请求为什么必须在中间件层返回，而不是放进去让模型拒绝？
7. 每 IP 都没超限为什么实例照样可能被刷爆？全局兜底为什么默认关闭、又为什么说清"进程内"？
