# Browser Provider 真实任务 E2E（v0.27 Batch 3）

> 脚本：`scripts/browser_provider_task_smoke.py`
> 环境：真实 Playwright Chromium（headless）+ 本地真实 HTTP 页面；**受控浏览器**，
> 不是用户当前标签页。

## 链路

1. 启动本地 HTTP 页面（表单：名字输入 + 提交按钮 + 结果区）；
2. `BrowserProvider` + `BrowserPolicy.secure([origin])`，工具注册进 `ToolRegistry`；
3. `ToolExecutor` 执行：`browser_open` → `browser_observe` → `browser_type` →
   重新观察（type 会生成新快照）→ `browser_click` → `browser_observe`；
4. 每次修改性动作都经 `Channel.confirm` 审批（本脚本用自动批准通道，记录审批次数）；
5. 从页面真实文本验证“完成：YAI”，写 `VerificationResult`；
6. `verify_receipt_chain` 校验本次 run 的执行收据链。

## 2026-11-03 实测

```json
{
  "approvals": 3,
  "page_text": "YAI Browser E2E\n名字 提交\n完成：YAI",
  "receipt_chain_ok": true,
  "verification": {
    "status": "verified_success",
    "confidence": 0.9,
    "evidence": [{"kind": "page_text", "summary": "页面真实文本包含完成结果", "ok": true}]
  },
  "ok": true
}
```

- 3 次审批对应 open / type / click（修改性动作，`BrowserPolicy.secure` 默认要求）；
- 旧引用过期由快照版本处理：type 后重新观察再点击，未用旧 ref 盲操作；
- 收据链完整；最终验收来自页面真实文本，不是模型自述。

## 与“当前标签页”的边界（ADR 0006）

- 本 E2E 操作的是 Browser Provider 自己的受控浏览器，**不是用户当前默认 Chrome
  标签页**；扩展 `activeTab + scripting` 仍只负责读取当前页只读快照。
- “授权当前标签页 → 直接写操作”依赖 Extension Backend / CDP 复用方案（ADR 0006
  的 A/B），尚未实现；在实现前不得把本 E2E 或“读取当前页”宣传成“能操作当前页”。


## 当前标签页写操作（v0.27 追加，opt-in）

在 ADR 0006 的 C 边界内新增一条**受限**路径，默认关闭：

- `initialize.current_tab_tool=true` 时 Sidecar 注册 `current_tab_action` 工具；
- 工具只接受 `action=click|type` + CSS `selector` + `value`，每次动作走
  `ToolExecutor` 审批；不执行任意 JS、不读未授权页面；
- 扩展面板只对用户点击“读取当前页面”时绑定的 `authorizedTabId` 执行
  `chrome.scripting`，不随后续焦点漂移；无 activeTab 授权时失败关闭；
- 协议级回归：`tests/test_sidecar_protocol.py` 覆盖“默认不注册 / 显式开启后
  审批 + 反向 tab_action/request + 工具结果”全链路。

**真实浏览器当前标签页 E2E 限制**：`activeTab` 授权必须来自用户手势，当前
Playwright 自动化把侧边栏作为普通标签页打开，无法模拟真实 Side Panel 的用户点击；
因此本环境只能验证协议链路与扩展静态行为，真实点击侧边栏的端到端仍需人工按
`docs/guides/browser-extension-bridge.md` 验收。在人工验收前，不得宣称“当前标签页写操作
已真机通过”。
