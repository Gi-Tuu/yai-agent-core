# 架构决策记录（ADR）

记录"为什么这样设计"的时点决策；编号自 0002 起（0001 未使用）。
新增 ADR 时同步本表；决策正文的验证状态变化用**后记**追加，不回改正文。

| 编号 | 标题 | 状态 | 一句话 |
|---|---|---|---|
| [0002](0002-runtime-auth-boundary.md) | Runtime 的认证与授权边界（sidecar + FastAPI Battery） | 已接受（2026-10-31，v0.25 B3） | 会话面与工具面的认证边界；公网部署必须显式鉴权 |
| [0003](0003-acc-boundary.md) | ACC 与原生治理的职责边界 | 已接受（2026-10-31，v0.25 B8） | ACC 降级为互操作 shim，判定原语只剩内核一份 |
| [0004](0004-browser-provider-boundary.md) | Browser Provider 与浏览器扩展的边界 | 已采纳（v0.26.0，2026-11-03 发布） | 浏览器执行走 Integration 层适配器，扩展只是 Host |
| [0005](0005-outcome-verification.md) | 执行收据与任务验收的边界 | 已采纳（v0.26.0，2026-11-03 发布） | 工具调用事实（收据）与目标达成事实（验收）分离，验收器由宿主提供 |
| [0006](0006-browser-execution-unification.md) | 浏览器能力的两条执行路径与统一边界 | Accepted（2026-11-03，v0.27 B3） | 扩展只读快照与受控操作并存，边界明确、不宣称统一 |
| [0007](0007-local-secret-storage.md) | 本地模型 Key 的存储边界与 SecretStore 方案 | Proposed（2026-11-03，v0.27 B4） | bridge.json 明文风险的治理方案；**加密尚未实现**，当前仍为明文落盘 |
| [0008](0008-directed-federation.md) | 有向父子核（Directed Federation）的边界 | **Proposed（2026-11-09，设计稿）** | 父核可调子核、子核无路反调；联邦在宿主层实现，内核仍单 agent |
