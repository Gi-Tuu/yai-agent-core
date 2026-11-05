# ADR 0005：执行收据与任务验收的边界

- 状态：已采纳（v0.26.0，2026-11-03 发布）
- 日期：2026-11-03
- 相关：`src/yai_core/verification.py`、`src/yai_core/types.py`（`Evidence` /
  `VerificationResult` / `RunResult.verification`）、`docs/IMPLEMENTATION_STATUS.md`

## 背景

Agent 调用了工具、模型说了"完成"，都不等于用户目标达成：点击提交 ≠ 提交成功、
写入文件 ≠ 产物有效、下载按钮返回 ≠ 下载成功。同时，执行层已经有一套
`ExecutionReceipt` 记录**每个工具调用发生了什么**（not_started / succeeded /
failed / unknown / partial）。如果再用收据的 `succeeded` 冒充"任务完成"，就是在
制造虚假确定性。

## 决策

1. **两套事实分开**：
   - `ExecutionReceipt`：工具调用的事实（是否执行、是否超时、副作用未知与否，
     后置条件是否确认）；
   - `VerificationResult`：任务目标的事实（`verified_success` / `verified_failure` /
     `partial` / `unknown` / `not_verifiable` + 置信度 + 证据）。
2. **验收器由宿主提供**（`OutcomeVerifier` 契约，async `verify(goal, evidence, metadata)`）。
   内核不内置领域规则、不内置视觉判断；`CompositeVerifier` 只做确定性合并
   （失败优先、全成功、部分、未知、不可判定）。
3. **证据优先确定性来源**：执行收据、工具显式上报的 `evidence`（浏览器页面元数据、
   桌面窗口/控件指纹等）、宿主自己查询的 API/文件/DOM 状态；视觉模型只能作为
   可选补充，契约不依赖它。
4. **旁路语义**：验收发生在 `DONE` 之前，但失败/异常**不改变**最终文本、策略与
   事件流结构，只降级为 `not_verifiable` 并发出 `verification_failed` 诊断；
   **绝不因为验收失败而自动重试工具**（可能重复副作用）。
5. **未配置 = None**：不配置 verifier 时 `RunResult.verification is None`，保持旧行为，
   不假装验收过。

## 后果

- 宿主想"证明任务完成"必须自己给出可检查的证据或 verifier；YAI 提供契约、证据
  收集与组合器，不替宿主发明业务验收标准。
- 浏览器/桌面 Provider 的动作结果带 `outcome` 与 `evidence`，天然成为验收输入；
  两者仍然只报告"可观察变化"，不越权宣称成功。
- 后续如需视觉验收，作为宿主 verifier 的一个实现接入，不进内核。
