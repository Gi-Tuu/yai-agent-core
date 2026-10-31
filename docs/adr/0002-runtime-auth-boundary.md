# ADR 0002 · Runtime 的认证与授权边界（sidecar + FastAPI Battery）

- 状态：已接受（2026-10-31，B3 批次）
- 关联：v0.25 审计 C-1（sidecar 零认证）/ D-1（默认权限）/ 挂起决策"线上 `/v1/agent/run` 无鉴权"

## 背景

YAI 的两个对外运行面：

1. **sidecar**（stdio NDJSON + JSON-RPC）：供任何语言的宿主以子进程方式嵌入。
   stdio 管道两端是**同一台机器上的可信父进程**——协议层没有网络暴露。
2. **FastAPI Battery**（`/v1/agent/run` 等 HTTP 端点）：可被部署为公网服务
   （Render 演示实例）。当前无任何鉴权——这是**挂起的产品决策**，不是疏忽。

## 决策

### D1 · 三层边界归属（写入协议文档）

| 层 | 归属 | 说明 |
|---|---|---|
| 网络接入认证 | **宿主**（或边缘代理） | sidecar 是 stdio，无网络面；HTTP 部署由宿主决定是否开启下述 D3 |
| 用户身份认证 | **宿主** | YAI 不内置账户体系；身份以 `Subject` 注入 |
| 工具执行授权 | **YAI**（治理闸） | 基于宿主注入的 Subject + 宿主声明的治理数据 |

### D2 · Subject 只能来自可信宿主，不能来自请求参数（sidecar）

- `initialize` 可携带 `subject`（会话级可信身份，来自宿主的认证体系映射）；
- 绑定后，`agent/run` 请求参数里的 `subject` **直接拒绝**
  （`INVALID_PARAMS`，防多租户部署下身份伪造）；
- 未绑定时保持既有语义：stdio 父进程是可信宿主，由它负责把已认证用户
  映射成 Subject；HTTP 场景**禁止**把 sidecar 的 stdio 桥接到不可信输入。

### D3 · FastAPI Battery：opt-in 令牌鉴权（已实现，启用与否属部署决策）

- 该能力 v0.7 起即存在（`batteries/fastapi_server/auth.py`）：部署方设置
  `YAI_API_KEY` / `YAI_API_KEYS` 后，`/v1/*` 要求 Bearer Key
  （`hmac.compare_digest` 常量时间比较），失败 401 且零业务副作用；
- `/health` 与 `/.well-known/yai-verification.json` 列入公开白名单
  （评审硬门槛永不禁用）；
- **本轮结论**：机制无缺口；Render 演示实例是否设置该 env 属部署决策——
  启用 = render.yaml 加一行 env + Redeploy，维持现状 = 演示口径如实声明。
  原先"线上无鉴权"挂起项据此收敛为部署决策，不再是代码缺口。

## 后果

- sidecar 协议向后兼容：不传 subject 的宿主零变化；绑定的宿主获得防伪造保证；
- FastAPI 部署多了一个开关，默认关闭；文档给出开启方式；
- 不引入账户体系、不做会话管理 UI（明确不做红线）。

## 参考

- host_e 的令牌先例（`YAI_TOOL_TOKEN`，非环回绑定必须带令牌）；
- 任务书 P0-3；v0.25 审计报告 C-1。
