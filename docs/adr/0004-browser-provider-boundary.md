# ADR 0004：Browser Provider 与浏览器扩展的边界

- 状态：已采纳（v0.26.0，2026-11-03 发布）
- 日期：2026-11-03
- 相关：`src/yai_core/integrations/browser/`、`src/yai_core/sidecar/native_bridge.py`、
  `extension/`、`docs/guides/browser-extension-bridge.md`

## 背景

v0.26 要让 YAI 能真正操作浏览器并验证结果，同时避免变成"第二个浏览器产品"或
Windows 专用 Agent。需要先回答：浏览器能力属于 Core、Integration、Battery 还是 Host？

## 决策

1. **Core 不 import 任何浏览器库**，`dependencies` 继续为空。
2. 浏览器执行能力落在 **Integration 层可选适配器** `yai_core.integrations.browser`：
   - `BrowserProvider` 只依赖一个 `BrowserBackend` Protocol；
   - 真实后端 `playwright_backend.py` 走可选 `[browser]` extra；
   - Provider 只生成 `ToolSpec`，注册进 `ToolRegistry` 后由 `ToolExecutor` 统一做
     参数校验、权限策略、审批、收据与事件——**不另建 Tool Bus、不另建权限系统**。
3. 元素引用带**快照版本**（`s<N>:<i>`），页面变化后旧引用一律过期并提示重新观察；
   动作只返回 `executed + outcome`，不宣称任务成功；任务级结论由 Outcome Verification
   契约（`VerificationResult`）单独表达。
4. 浏览器扩展是 **Host**，不是 Agent Core：
   - `extension/` 只做侧边栏 UI 与连接管理，不跑 Agent 循环；
   - 通过 **Native Messaging Bridge**（`native_bridge.py`）连本地 Sidecar；
   - 不修改 Sidecar 公开协议（仍是 stdio NDJSON + JSON-RPC 2.0）；
   - 浏览器端不保存任何模型凭据，`initialize` 参数只来自本地 Bridge 配置。
5. 扩展操作白名单 + 来源校验 fail closed；出站超限帧截断；断连后结果标 `unknown`，
   不自动重放可能已有副作用的操作。

## 后果

- 不装 `[browser]` 的用户完全不受影响；浏览器/扩展能力不是内核必需项。
- 真实 Playwright 行为与浏览器扩展生命周期仍需在真机上验收，代码与文档明确标注
  「已实现·未真机验证」，不用离线测试冒充真机通过。
- 后续若增加 Windows Desktop Provider，复用同一套 `ToolExecutor` + 收据 +
  Outcome Verification 契约，不为平台新增第二套治理链路。
- 后记（2026-11-05）：真实 Chromium 冒烟与受控浏览器 E2E 已完成
  （`docs/evaluations/browser-task-e2e.md`），扩展真机验收记录见
  `docs/guides/browser-extension-bridge.md`；真实站点多样性、嵌套 frame、下载/上传与
  安装包分发仍未验证。
