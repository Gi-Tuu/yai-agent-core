# 逐行讲解 52 · 执行收据与进程内幂等

> 对应源码：`types.py::ExecutionReceipt`、`tools/executor.py`、
> `ToolGovernance.idempotency_key`、`kernel/loop.py` 的 `call_id` 透传。
>
> 这一篇回答一个运维问题：**工具调用失败后，到底能不能重试？**

## 0. 为什么不能只看 `ok=False`

一次工具调用可能处在这些状态：

- 参数校验失败：**没有进入 handler**，没有副作用；
- 权限/频率拒绝：同样没有执行；
- handler 正常返回：成功；
- handler 抛错：可能已经改了一半数据；
- 超时：同步步骤可能仍在后台跑完；
- 组合工具第 2 步失败：第 1 步的副作用已经落地。

把这些都压成一个 `ok=False`，模型只会原样重试，运维也无法判断该查什么。

## 1. `ExecutionReceipt`

```python
@dataclass(frozen=True)
class ExecutionReceipt:
    call_id: str
    tool: str
    status: Literal["not_started", "succeeded", "failed", "unknown", "partial"]
    retryable: bool
    idempotency_key: str
    error: str
    duration_ms: float
    replayed: bool
```

状态口径：

| 状态 | 含义 |
|---|---|
| `not_started` | 参数/权限/频率/预览等在 handler 之前失败，没有副作用 |
| `succeeded` | handler 正常返回 |
| `failed` | 执行器明确知道失败（如沙箱返回 `ok=False`） |
| `unknown` | 超时、同步 handler 被放弃、handler 抛错、沙箱调用异常——可能已有副作用 |
| `partial` | 组合工具前几步成功、后续步骤失败 |

收据随 `TOOL_RESULT.data["receipt"]` 流出；`call_id` 来自模型工具调用 id，
组合步骤使用 `父 call_id:步骤下标`。

## 2. `retryable` 不是“建议重试”

```python
retryable = bool(idempotency_key) and status in {"failed", "unknown", "partial"}
```

含义是：**有宿主提供的稳定幂等键时，重试不会重复执行**。没有 key 时，
`unknown` / `partial` 一律 `retryable=False`——这正是“超时后不要盲目重试写操作”。

## 3. 宿主提供幂等键

```python
tool.governance = ToolGovernance(
    idempotency_key=lambda args, subject: f"order:{args['order_id']}:refund",
)
```

- 必须由宿主生成，且对“同一次业务操作”稳定；
- 返回空串表示不做去重；
- 生成器失败会失败关闭（`not_started`），不能假装可去重；
- key 是函数，不随 manifest 导出。

## 4. 进程内去重

`ToolExecutor` 维护一个有界缓存（默认 256 条）：

```text
第一次：key -> 执行 -> 缓存 (ok, text, status)
第二次：key 命中 -> 复核当前准入 -> 返回缓存，事件标记 replayed=True，**不进入 handler**
```

边界：

- 只覆盖**当前进程**；多实例 / 跨进程必须由宿主用同一个 key 在共享存储中去重；
- 缓存 `unknown` / `partial` 是为了让盲目重试也拿回同一个不确定结果，而不是再次执行；
- 宿主若要用新 key 重试，必须自己确认副作用状态；
- **重放不会重新进入 handler，也不会重新走人工审批**——同一 key 被视为同一笔
  已批准操作。但**当前策略的 DENY 仍然生效**：缓存是去重，不是绕过权限收紧的后门；
- 缓存条目绑定**参数指纹**：同一个 key 换了参数会被拒绝（`idempotency_conflict`，
  `not_started`），不会把上一组参数的结果当成这次的结果返回；
- 权限会随运行期变化或多租户的宿主，应把主体/权限版本纳入 key，或关闭 idempotency。

### 组合缓存必须保留真实调用的权限依赖

只复核外层组合名的 DENY 不够：外层缓存可能含有现已撤权的子工具结果。
执行器用私有 trace 记录工具注册对象身份、名称以及**当次已解析参数的独立深拷贝**，
与正文一起存入现有 LRU 条目。它记录已进入的子调用（包括失败或尚未完成的调用），
不是对当前步骤模板做一次静态展开：

- `$input` / `$steps` 引用以首次 `resolve_args` 得到的真值为准；不从脱敏事件还原，
  也不为了取前一步输出重新执行工具。handler 改写自己的参数副本不影响依赖快照；
- 嵌套组合共用 trace；子调用命中已有缓存时也向父调用传播该缓存的历史依赖；
- 进入子调用时立即记录，因而组合超时取消、partial、异常及截断正文仍保有此前依赖。
  同一个工具被不同参数调用多次，每次都单独保留，不按名称去重。

重放保留原有外层 DENY 检查，并用**本轮 subject** 复核历史依赖的当前 policy DENY、
函数 gate reject、声明式 approval 的拒绝及参数约束；注销或以新 `ToolSpec` 同名替换
也拒绝。复核回调拿独立副本，不能改写缓存参数。无法复制参数或子依赖复核异常时
失败关闭；拒绝消息不包含真实参数、gate 拒绝详情或异常正文。

依赖拒绝产生 `TOOL_RESULT`（`replay_denied=True`）及本轮 `not_started` 收据；
不交付缓存敏感正文、不删除条目后回退重执行，也不执行任何 handler。允许重放时，
ASK / `require_approval` **不视为 DENY**，仍不弹审批、不生成新预览、不调用子幂等键
生成器、不重新计子步骤的执行预算/频率，不刷新子代码工具 TTL；这里是结果交付准入，
不是模拟一次完整执行。

实际边界：依赖含未脱敏参数，仅留在当前执行器内存中，与正文随 LRU 淘汰，不进入
事件、收据或 manifest；256 条是条目数上限，并非参数字节数上限。身份判断是注册对象
的 `is` 比较，不是全局权限版本或注册历史：同一个对象原地更换 handler、注销后又
注册同一对象不会被当作新身份。当前治理仍会复核，但不承诺跨进程失效或并发授权变更
的原子快照。宿主仍须维护主体隔离的幂等键，并保证 policy/gate 等准入回调无业务副作用。

### 重放的事件副本仍须脱敏

缓存里的 `text` 是**给模型的正文**，不是第一次 `TOOL_RESULT.preview` 的脱敏副本。
因此命中缓存后不能直接用 `cached_text[:200]` 当预览：首次执行已经隐藏的秘密，
会在第二次调用时重新进入事件流。

重放通过 `_replay_preview` 按**当前工具治理**重新处理：

- `audit_sensitive=True`：沿用 `_event_preview`，整体折叠结果；
- 声明 `sensitive` 路径：先解析完整缓存 JSON，再沿用路径脱敏，最后截取预览；
- 正文可能已被长度上界截成无效 JSON；解析失败或不是对象时，只输出“预览不可用 +
  字符数”，绝不退回明文前 200 字符。不能靠重新解析被截断的 JSON 来保证脱敏成功；
- 宿主在两次调用之间开启审计敏感标记、增加敏感路径时，新声明立即作用于重放事件，
  不会因为此前已缓存而失效；未声明治理时仍维持原有预览行为。

失败正文通常是非结构化异常文本，无法可靠按字段路径处理。因此只要声明了上述任一
敏感治理，首次与重放的错误事件副本、`receipt.error` 都整体折叠，避免错误信息或
收据成为旁路。无声明时保留原错误诊断；给模型的错误正文与缓存正文都不改。

这套处理覆盖普通、代码与组合工具的缓存结果，不改变 `status`、`retryable`、幂等键
或执行次数；重放仍使用新的 `call_id` / 耗时并标记 `replayed=True`。治理收紧时，
收据的错误**展示副本**也随之收紧。组合子步骤的事件仍按各子工具自己的治理声明处理，
组合工具的声明不替代子工具声明。

## 5. 模型侧话术与工具侧收据

内核在超时/未知结果时已经提示“可能仍在后台跑完，重试前先确认状态”；
收据把同一事实变成机器可读字段，宿主可以把 `status=unknown` 接到告警、人工工单
或业务状态查询，不必解析自然语言。

## 5.5 收据串成哈希链（防篡改）

每条收据带两个字段：

- `prev_hash`：上一条收据的 `receipt_hash`；第一条是空串；
- `receipt_hash`：本条收据全部事实（含 `prev_hash`）的 SHA-256。

宿主导出一批收据后，用 `verify_receipt_chain(receipts)` 一次校验：首条 prev 为空、
逐条指向前一条、自身哈希可重算。**改一个字段 / 抽掉中间一条 / 调换顺序**都会返回 False。
这是把"审计日志"升级成"可验证审计"的最小代价做法（思路对齐 IETF Agent Audit Trail 草案的
`prev_hash`），不引入任何依赖，也不改变单条收据的字段语义。

## 6. 自检

1. 为什么 `not_started` 与 `unknown` 必须分开？
2. `retryable=True` 的前提为什么是“有 idempotency_key”，而不是“工具是读操作”？
3. 进程内去重为什么不能承诺跨实例语义？
4. 组合工具第 2 步失败时，收据为什么是 `partial` 而不是 `failed`？
5. 收据和 `TOOL_RESULT.preview` 各自解决什么问题？
