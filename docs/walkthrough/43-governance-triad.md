# 逐行讲解 43 · 治理三件套：令牌鉴权、参数快照、声明式脱敏

> 第 22/23 篇把员工做成了原生桌面端、一座岛管多个专员。本篇补的是**跨进程之后
> 才暴露出来的安全缺口**：当"审批在灵动岛侧、执行在宿主侧"变成两个进程，
> 服务端那道口若谁都能调，前面所有权限设计都会被绕过。
> 三件事各自独立，但共同回答一个问题：**审批看到的东西，凭什么等于真正执行的东西？**

## 0. 三个缺口是怎么被发现的

本轮通用能力与兼容性架构审查逐文件取证时发现：

| 缺口 | 证据 | 后果 |
|---|---|---|
| 跨进程工具口无鉴权 | `web_app.py:322` 直接执行，`tool_service.py:13` 自陈"决策在调用方" | 同机任意进程可绕过审批执行宿主写操作 |
| 审批与执行共用一个 dict | `authorize.py` 把 `arguments` 同时交给 `confirm()` 与后续执行 | 通道在确认期间改参数，改的就是实际执行值 |
| 事件原样携带参数 | `authorize.py` 的 `PERMISSION_ASKED`/`TOOL_CALL`、`otel.py:116` 的 span | 密钥作参数即进日志/追踪/界面 |

## 1. 令牌鉴权（`examples/host_e_sales_crm/tool_auth.py`）

解析优先级：**显式参数 → 环境变量 `YAI_TOOL_TOKEN` → 本地令牌文件**
（`data/host_e/tool_token`，首次启动 `secrets.token_urlsafe(32)` 生成并 `chmod 600`）。

```python
token_auth.token_matches(header, expected)   # 只认 Bearer，hmac.compare_digest 常量时间
token_auth.startup_guard(host, explicit)     # 非环回 + 无显式令牌 → 拒绝启动
```

三条刻意的设计：

1. **白名单清单端点也要令牌**：`GET /api/agent/tools` 返回的白名单本身就是攻击面信息；
2. **纯 CRM 自己的页面接口不受影响**：`/api/state` 等照旧无令牌——它是普通软件，
   不是 Agent 口，鉴权只加在"能驱动 Agent 能力"的边界上；
3. **边界写进 docstring**：能读到令牌文件的进程即被视为同一用户身份。这一层挡的是
   同机其他用户/服务与误接客户端，**不防同一用户的恶意本地进程**（那要 OS 级隔离）。

服务端与客户端各持一份令牌文件路径常量（跨进程不能共享代码），由
`tests/test_host_e_tool_auth.py::test_client_and_server_share_token_convention`
断言二者一致，防止哪天改一边。

## 2. 参数快照：三份副本，一份权威

`snapshot_arguments()` 深拷贝模型参数（模型侧本就是 JSON 值，拷贝代价低；
拷不动的极端情况**退回原对象**——快照是加固手段，不该变成新的崩溃源）。

关键在于**谁拿哪一份**：

```
模型给的 dict
   └─ executor 入口 snapshot → arguments（权威：策略判定 + 实际执行）
        ├─ redact_arguments(...) → shown   → PERMISSION_ASKED 事件 + channel.confirm()
        └─ redact_arguments(...) → audit   → TOOL_CALL 事件
```

于是：通道在 `confirm()` 里 `arguments.clear()` 也只清掉自己那份副本；宿主改审计事件
也改不动下一份。**"用户批准的是 A、执行的是 B"这条路径被结构上堵死**，
而不是靠"约定通道别乱改"。

`replan` 那条路径**故意不快照**：它不过授权闸（只重排内部计划、无外部副作用），
给它加快照会留下一句错误的注释——注释与语义不符比没有注释更糟。

## 3. 声明式脱敏：`ToolSpec.governance.sensitive`

```python
spec.governance = ToolGovernance(sensitive=("api_key", "headers.Authorization"))
# 事件与 confirm 里 → "[redacted:len=22]"；handler 收到的仍是真值
```

为什么按**声明**而不是按字段名猜：

- 猜会漏（改叫 `tok_v2` 就绕过），也会误伤（把业务字段抹掉，审批人反而看不懂）；
- 任务书与 ACC 规范同一条纪律：安全语义必须来自可信声明，不能来自启发式；
- 所以"未声明即不脱敏"是**有意的边界**，测试
  `test_undeclared_params_stay_visible_by_design` 把它钉成契约而不是留成惊喜。

占位符保留长度（`len=N`）：审批人需要判断"这是不是一把我认得的长令牌"，
全抹成一个 `***` 反而丢信息。

脱敏只发生在**对外副本**上，因此追踪 span、metrics 标签、事件流自动继承——
不需要在每个出口各写一遍。

## 4. 自检

1. 为什么权限决策在调用方（灵动岛）完成时，服务端反而**必须**鉴权？
2. 三份参数副本各自的作用是什么？少了"审批展示副本"会出什么问题？
3. 为什么 `replan` 不做快照？
4. 为什么内核不按字段名关键词自动脱敏？这条边界的代价是什么？
5. 令牌文件方案挡住了谁、挡不住谁？要挡后者该换什么机制？
