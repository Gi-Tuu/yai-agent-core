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
第二次：key 命中 -> 直接返回缓存，事件标记 replayed=True，**不进入 handler**
```

边界：

- 只覆盖**当前进程**；多实例 / 跨进程必须由宿主用同一个 key 在共享存储中去重；
- 缓存 `unknown` / `partial` 是为了让盲目重试也拿回同一个不确定结果，而不是再次执行；
- 宿主若要用新 key 重试，必须自己确认副作用状态；
- **重放不会重新进入 handler，也不会重新走人工审批**——同一 key 被视为同一笔
  已批准操作。权限会随运行期变化或多租户的宿主，应把主体/权限版本纳入 key，
  或关闭 idempotency。

## 5. 模型侧话术与工具侧收据

内核在超时/未知结果时已经提示“可能仍在后台跑完，重试前先确认状态”；
收据把同一事实变成机器可读字段，宿主可以把 `status=unknown` 接到告警、人工工单
或业务状态查询，不必解析自然语言。

## 6. 自检

1. 为什么 `not_started` 与 `unknown` 必须分开？
2. `retryable=True` 的前提为什么是“有 idempotency_key”，而不是“工具是读操作”？
3. 进程内去重为什么不能承诺跨实例语义？
4. 组合工具第 2 步失败时，收据为什么是 `partial` 而不是 `failed`？
5. 收据和 `TOOL_RESULT.preview` 各自解决什么问题？
