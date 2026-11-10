# ADR 0006：浏览器能力的两条执行路径与统一边界

[English](0006-browser-execution-unification.en.md) · **简体中文**

- 状态：Accepted（2026-11-03，v0.27 Batch 3）
- 关联：`src/yai_core/integrations/browser/`、`extension/`、`src/yai_core/sidecar/native_bridge.py`、
  `docs/guides/browser-extension-bridge.md`、`docs/IMPLEMENTATION_STATUS.md`
- 关联任务书：v0.27 §5（Browser Extension 与 Browser Provider 执行路径统一）

## 背景

仓库里同时存在两条浏览器能力路径：

1. **Browser Provider（受治理路径）**：`BrowserProvider` + `BrowserBackend` +
   `PlaywrightBrowserBackend` + `BrowserPolicy`；工具注册进 `ToolRegistry`，统一经
   `ToolExecutor` 做参数校验、权限、审批、收据与事件；快照引用、来源闸、密码框拒绝、
   Playwright 路由层重定向 abort 都在这一层。
2. **浏览器扩展路径**：MV3 Side Panel + `chrome.scripting` 读取当前活动标签页 +
   Native Messaging Bridge + Sidecar + Playwright MCP（可选 `--cdp-endpoint`）。

两条路径的**真实能力并不相同**：

- 扩展的 `activeTab` + `chrome.scripting` 只能在用户点击后**读取**当前标签页，得到的是
  **只读、不可信页面快照**；它没有 DOM 写权限，也不会替用户点击当前标签页。
- 实际的浏览器自动化由 Playwright MCP 执行：默认启动自己的浏览器，或用 CDP 连接一个
  **独立 profile** 的 Chrome；这不等同于接管用户当前默认浏览器会话。
- MCP 的 `browser_browser_*` 工具是通用 Playwright 工具，**不自动继承** `BrowserPolicy`
  的来源/密码框/审批语义；真正能保证 `BrowserPolicy` 的是 Browser Provider 路径。

因此"读取了当前页面"不能推导为"Agent 能操作当前页面"，也不能把两套工具同时开放后
宣称已经统一。

## 决策

**采用 C（并存 + 明确边界），同时保留向 A/B 演进的接口。**

- **C 现状**：
  - 扩展侧边栏只承诺：读取当前标签页的**只读快照**，作为不可信上下文附加到任务；
    面板必须明确标注"只读、不会操作当前标签页"。
  - 受控浏览器操作只走两条显式后端之一：
    - Browser Provider（`BrowserPolicy` 治理；推荐生产使用）；
    - Playwright MCP（宿主显式配置；生产必须用 `ToolExecutor` 白名单/审批与
      host 侧进程隔离，不能假设它继承了 `BrowserPolicy`）。
  - 两者不共享工具名、不互相回退；面板显示当前"受控浏览器"是否接入。
- **演进方向**：
  - A（Browser Provider 成为唯一执行入口，扩展实现 `BrowserBackend`）：需要
    `chrome.debugger` / `scripting` 的写操作授权模型与逐动作审批，工作量较大；
    在完成前不得宣称"扩展已能操作当前标签页"。
  - B（保留 MCP，但通过统一浏览器策略适配器接入）：需要把 MCP 通用工具参数映射到
    `BrowserPolicy`（来源、敏感字段、审批），当前 MCP schema 不足以可靠实现，
    暂不采用。
- **决策门槛**：A/B 任一方案落地前，必须先给出权限一致性测试（同一动作在两条路径
  得到相同的 DENY/审批/来源结论）与真实标签页 E2E；否则保持 C。

## 后果

- 用户必须能看到三种状态：`当前标签页（只读快照）`、`受控浏览器（Browser Provider）`、
  `受控浏览器（MCP / CDP）`；面板在未接入时明确说"未连接受控浏览器"。
- 文档/README/IMPLEMENTATION_STATUS 必须区分"读取"与"操作"，不得用真机读页成功
  代替操作验收。
- 生产建议：受治理操作用 Browser Provider + `BrowserPolicy.secure()`；MCP 只作为
  宿主显式配置的可选执行后端，工具白名单/审批要在宿主层收紧。
- 不新增第二套浏览器协议；Bridge 只转发 Sidecar/MCP 事件，不直接执行页面动作。

## 被否决的方案

- **A 立即实施**：扩展写操作权限、逐动作审批、CDP/activeTab 语义差异未解决，
  会制造"看似统一、权限却不同"的假象。
- **B 立即实施**：MCP 工具 schema 与 BrowserPolicy 语义不匹配，无法保证来源/密码框
  一致性；强行包装只会让审批落空。
- **同时开放两套工具并称为统一**：明确禁止；这正是本 ADR 要消除的风险。

## 验收

- 面板对"只读快照"与"受控浏览器"有可见区分（静态测试钉住 DOM/文案）。
- `docs/guides/browser-extension-bridge.md` 明确写出"读取当前标签页 ≠ 操作当前标签页"。
- 后续 A/B 实施必须附权限一致性测试与真实 E2E；未完成前保持本 ADR 的 C 边界。
