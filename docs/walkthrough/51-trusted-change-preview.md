# 逐行讲解 51 · 可信变更预览：审批人看真实差异，执行前复核计划

> 对应源码：`types.py::ChangePreview`、`ToolGovernance.preview`、
> `policy/authorize.py`、`tools/executor.py`、`shell/desktop/channel.py`。
>
> 这一篇解决最后一公里：**审批人批准的不是工具名，而是宿主渲染的实际变更。**

## 0. 问题：审批卡上只有工具名和参数

`transfer(amount=5000)` 不足以让人判断：

- 这 5000 是从哪个账户转到哪个账户？
- 目标账户当前余额、风控状态、是不是本人？
- 这次操作会改哪条记录、改前改后是什么？

如果这些由模型自己描述，审批人看到的是"模型的解释"，不是"宿主的真实状态"。
可信变更预览把生成权交给宿主，内核只负责**传递、展示、复核**。

## 1. 数据结构

```python
@dataclass(frozen=True)
class ChangePreview:
    summary: str          # 一句话说明这次要改什么
    token: str            # 宿主计算的版本/哈希；内核只比较
    diff: str = ""        # 实际差异（宿主渲染）
    target: str = ""      # 落点
```

`token` 是复核的关键：它通常由"目标当前内容 + 本次参数"计算。内核不解释 token，
只比较审批前后两次生成的结果；token 变了，就说明审批依据已经失效。

## 2. 宿主声明预览生成器

```python
def preview_transfer(arguments, subject):
    account = backend.get_account(arguments["account_id"])
    new_balance = account.balance - arguments["amount"]
    return ChangePreview(
        summary=f"从 {account.name} 转出 {arguments['amount']} 元",
        token=backend.revision(account.id),
        diff=f"余额：{account.balance} -> {new_balance}",
        target=f"account/{account.id}",
    )

tool.governance = ToolGovernance(
    approval=ApprovalPolicy(required=True),
    preview=preview_transfer,
)
```

`preview` 是**函数**，和 `gate` 一样：

- 不随能力清单导出，外来 manifest 不能伪造本地预览器；
- 模型参数无法设置它；
- 支持同步/异步 provider；
- 生成失败时失败关闭，不会"没预览也照样弹审批"。

## 3. 执行链

```text
ToolExecutor.execute
  ├─ schema/gate/rate/budget
  ├─ 生成 preview ──失败──> TOOL_RESULT(preview_failed)，不执行
  ├─ authorize_tool_call(..., preview=preview)
  │    ├─ PERMISSION_ASKED 带 preview
  │    ├─ Channel.confirm(..., context={"preview": ...})
  │    └─ TOOL_CALL 带 preview
  ├─ 审批通过
  ├─ 再次生成 preview
  │    ├─ token 不同 ──> TOOL_RESULT(preview_stale)，不执行
  │    └─ token 相同 ──> 继续
  └─ handler
```

组合工具自己的审批走同一条规则；内部步骤各自声明的预览也分别生效。

## 4. 界面

- CLI：按 y 之前打印"变更预览 / 差异"；
- 桌面 `QtChannel`：把 `preview` 并入卡片负载；
- `Panel.qml`：`pendingSummary` 追加 summary、diff、target；
- `PERMISSION_ASKED` / `TOOL_CALL`：审计链保留实际预览。

## 5. host_e 的落地

`examples/host_e_sales_crm/agent_bridge.py` 在 `AgentCore.auto(...)` 之后调用
`attach_write_previews(core, crm)`，只给写工具挂 preview 闭包：

- 预览读当前 CRM 数据，渲染客户、跟进、订单、待办等实际变化；
- token 是“当前数据 + 参数”的 SHA-256 摘要，确认后任一侧变化都会换 token；
- `WebChannel` 声明 `yai_approval_context_aware = True`，把 `preview` 并进网页
  `permission_request` 负载；
- 预览器不调用写方法，只读业务对象；真实系统应换成自己的事务快照/revision。

桌面销售专员是**跨进程**路径：`HostECrmClient.preview()` 调用 host_e 新增的只读
`POST /api/agent/preview`，由 host_e 的 `render_write_preview()` 渲染同一份 diff；
桌面 Core 仍用返回的 token 做执行前复核。这样网页 AI 与桌面 AI 共用一套业务预览规则，
不会出现“两边看到的 diff 不一样”。

两个实现细节：`HostECrmClient._preview` 必须是私有方法，否则 `AgentCore.auto()`
会把它当成业务工具注册给模型；`preview_stale` 会**立即终止当前任务**，不再让模型
继续调工具补救——继续补救既不会恢复审批，也容易把免费模型的配额打满。

## 6. 诚实边界

- token 由宿主计算；宿主返回常数 token，内核无法发现状态变化；
- 内核只做"审批前后两次比较"，无法消除**第二次生成到 handler 执行之间**的
  宿主状态变化；宿主应在自己的写事务里做最终一致性检查；
- 预览是宿主代码，内核不执行模型生成的预览文本；
- 预览可能含敏感数据：宿主应只渲染审批人有权看到的内容，并自行做脱敏。

## 7. 自检

1. 为什么预览生成器放在 `ToolGovernance` 而不是让模型填一个 `preview` 参数？
2. token 变与 preview 文本变，哪个才是内核拒绝执行的依据？为什么？
3. 预览生成失败时为什么不能退回"只显示参数"继续审批？
4. 第二次生成与 handler 之间还剩什么竞态？应该由谁兜底？
5. 组合工具的预览与内部步骤的预览是什么关系？
