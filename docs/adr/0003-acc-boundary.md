# ADR 0003 · ACC 与原生治理的职责边界

- 状态：已接受（2026-10-31，v0.25 B8 批次）
- 关联：10-16 路线 2 三步（原生审批转正、ACC 降级 translator）、v0.25 任务书 6.3

## 背景

ACC（Agent Capability Contract）曾在历史上承担参数级审批的判定原语。
路线 2 三步（PR #63/#64/#65，2026-10-16）之后，判定原语已全部原生：
`policy/approval.py` 承载审批判定，`ToolGovernance` 承载数据，ACC 层只剩翻译。

## 现状职责盘点（2026-10-31 实测）

| 职责 | 归属 |
|---|---|
| 审批判定原语（九算子、严格 JSON 比较、fail-closed） | **YAI 原生**（policy/approval.py，ACC 侧为同构别名） |
| 风险/作用域枚举 | **YAI 原生**（ToolGovernance.risk/scope，落 PERMISSION_ASKED/TOOL_CALL） |
| 资源作用域闸 | **YAI 原生**（filesystem_gate + paths.py 单一真源） |
| 超时/频率强制 | **YAI 原生**（execution.timeout_ms/rate_limit 翻译进治理数据） |
| OpenAPI `x-agent-capability` 声明解析与映射 | **ACC translator**（integrations/acc/，声明不可用整条操作跳过） |
| 26 条官方向量一致性测试 | **ACC translator 的测试资产**（sdist 已排除分发，缺文件整模块 skip） |

## 决策

1. **继续维护 ACC 为可选 OpenAPI 互操作 translator**，不删除：
   - 它是 v0.10.0 起公开 manifest 格式的一部分（`governance.acc` 字段），删除是破坏性变更；
   - 26 条 Apache-2.0 官方向量是唯一的外部一致性证据；
   - 内核运行时**零依赖** ACC（路线 2 步骤 3 已验证：子进程 import 内核不出
     `yai_core.integrations`）。
2. **判定逻辑只养一份**：ACC 的 `AccApprovalCondition` 是 `ApprovalRule` 别名，
   两条路径的 parity 由参数化测试钉住；新增算子只进原生层。
3. **Profile 口径**：只声称 OpenAPI Binding Parser Profile；Runtime Profile 所需的
   按主体资源级授权已由原生 filesystem_gate 覆盖，不重复声称。

## 后果

- 新宿主不需要了解 ACC 即可获得全部治理能力（原生数据驱动）；
- 需要 ACC 互操作的宿主加载声明即得同强度治理（只能加严）；
- 删除 ACC 的成本（破坏 manifest 兼容 + 失去外部一致性证据）大于维护成本，
  维护面已收敛到"翻译器 + 向量测试"两个文件群。
