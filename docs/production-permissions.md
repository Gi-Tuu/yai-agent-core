# 生产环境权限配置指南

> 对应 v0.25 审计 D-1：`AgentCore` 的 `auto_approve_tools=True` 默认值是**开发形态**
> （等价于 `AllowlistPolicy(mode="allow_all")`，所有工具免审批自动执行）。
> 生产 / 多用户环境必须显式配置权限策略。本页所有代码可在
> `examples/production_policy.py` 直接运行（有测试引用，不会文档漂移）。

## 一、最小安全配置（复制即用）

```python
from yai_core import AgentCore
from yai_core.policy import AllowlistPolicy
from yai_core.types import ToolGovernance

# 1) 白名单：只放行确定无副作用的读操作；其余工具一律走审批或拒绝
policy = AllowlistPolicy(
    ("search_notes", "list_notes", "get_weather"),  # 显式白名单
    mode="auto",  # 白名单内自动放行；名单外进入审批通道
)

core = AgentCore(
    model,                       # 你的模型提供方
    policy=policy,               # 显式策略（传入后不再触发生产安全警告）
    auto_approve_tools=False,    # 语义声明：这不是 allow_all 开发形态
)
```

## 二、写操作与外发：逐项声明治理

```python
spec = build_spec(send_direct_message)
spec.governance = ToolGovernance(
    # 参数级审批：amount > 1000 的调用必须人工确认（ApprovalRule 是数据）
    approval=ApprovalPolicy(when=(ApprovalRule("amount", ">", 1000),)),
    # 外发文本参数：URL 来源闸只信任务原文
    egress_text=("body",),
    # 资源作用域 / 超时 / 频率上限按需声明
)
core.register_tools([spec])
```

要点（详见 `docs/walkthrough/43-governance-triad.md`）：

- 治理是**数据**：随能力清单跨进程搬运，导入端不需要任何外部协议即恢复强制；
- 外部声明只能把闸门**往上抬**（ALLOW → 人工确认），永远抬不动 DENY；
- 脱敏按 `governance.sensitive` 声明生效，未声明的字段不会被猜着脱敏；
- 同步工具超时是"给模型的止损"，不是进程级立即脱身（见讲义 43）。

## 三、Subject：可信身份只来自宿主

```python
def subject_provider() -> Subject:
    # 由宿主的认证体系（如 AMBRACE 的用户系统）提供，内核不内置账户体系
    return Subject(id=current_user.id, ...)
```

- 模型参数**无法**伪造 Subject（内核不从工具参数读身份）；
- 多用户隔离：不同用户使用不同 AgentCore 实例，或在宿主侧按会话注入 Subject；
- 实例级任务串行（同一 Core 内任务不交错）；并发请多实例。

## 四、上线前自查清单

- [ ] `auto_approve_tools` 显式设置（默认值会触发 ProductionSafetyWarning）
- [ ] 写操作 / 外发 / 资源类工具都有 `ToolGovernance` 声明
- [ ] 白名单外工具的审批通道有人接（Channel.confirm 的宿主实现）
- [ ] `YAI_API_KEY` 等公网暴露面已按部署文档评估（见 `docs/competitions/deployment.md`）
- [ ] 预算三道墙（模型 / 工具 / token）按需配置（`Budget`）
