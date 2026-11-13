# YAI 侧边栏真实 Side Panel 人工验收清单（v0.27）

[English](sidepanel-manual-acceptance.en.md) · **简体中文**

> 自动化无法授予 `activeTab`（必须真实用户手势），以下步骤必须在**真实 Chrome/Edge
> 侧边栏**中人工完成；截图/事件流/日志作为证据，不能只看最终回答。

## 前置

- [ ] 已重载扩展（manifest 变更后必须 Reload）
- [ ] 通过工具栏图标打开 **Side Panel**，不是 `chrome-extension://.../sidepanel.html` 标签页
- [ ] Bridge 状态“协议握手完成”，模型/MCP 状态符合预期
- [ ] 测试用普通 http/https 页面，不含密码/支付/真实账号操作
- [ ] 若测当前页写操作：`bridge.json` 的 `initialize.current_tab_tool=true`，已重启 Sidecar

## A. 当前标签页只读

| # | 操作 | 预期 |
|---|---|---|
| A1 | 打开测试页，点“读取当前页面” | 首次弹站点权限；允许后显示标题/URL/正文预览 |
| A2 | 拒绝站点权限 | 明确提示“拒绝读取”，页面内容不进入任务 |
| A3 | 点“运行”并附加快照 | 面板显示“只读快照”；最终回答可引用正文 |
| A4 | 切换到 `chrome://` 内部页后读取 | 明确拒绝，不尝试注入 |

## B. 受控浏览器 / CDP

| # | 操作 | 预期 |
|---|---|---|
| B1 | 配置 CDP 9222 后运行“打开页面并读取标题” | browser_* 工具正常；最终标题正确 |
| B2 | 关闭 9222 Chrome 后重启侧边栏 | 启动器应自动拉起；失败时回退 MCP 自管浏览器（日志可见） |
| B3 | 页面重定向到未授权 origin | 加载前被拦截，未授权内容不进入上下文 |

## C. 当前标签页写操作（opt-in）

| # | 操作 | 预期 |
|---|---|---|
| C1 | 读取页面授权后，运行“用 current_tab_action 输入并点击” | 每次 click/type 前出现审批卡；拒绝则不执行 |
| C2 | 允许审批 | 页面真实 DOM 变化；重新观察能看到结果 |
| C3 | 拒绝审批 | 页面不变，事件流有拒绝记录 |
| C4 | 操作后检查收据/验证 | 工具结果有 receipt；页面文本为真实证据 |
| C5 | 点击期间切换标签页 | 仍只操作已授权的 `authorizedTabId`，不误操作新标签页 |

## D. 生命周期 / 异常

| # | 操作 | 预期 |
|---|---|---|
| D1 | 运行中取消 | 任务终止，不承诺副作用回滚；终态明确 |
| D2 | 关闭侧边栏 | Bridge 断开；重开后结果视为 unknown，不自动重做 |
| D3 | Service Worker 被回收/重连 | 面板恢复连接；运行位不永久占用 |
| D4 | 审批超时 | 按拒绝处理，不无限挂起 |
| D5 | Bridge 断连 | 明确错误，不显示“假成功” |

## 执行环境（可选）

没有本机交互桌面时，可用云端 Linux 桌面执行本清单（**仅适用于浏览器 / Side Panel**，
不能替代 Windows UIA 验收）：

- **box by Simular**（<https://box.simular.ai/>）：云端 Ubuntu 桌面 + Chrome + shell，
  通过 MCP 或 VNC 操作；每账号一台免费云桌面（官方口径，计费可能变化，以
  <https://box.simular.ai/billing> 为准）；空闲休眠、文件保留，**连续 14 天不用会删机**；
  不提供稳定公网端口，不能当公网服务器。
- 接入方式：box 的 MCP 端点需要 OAuth 或个人 key（`Authorization: Bearer cb_...`）。
  YAI 内核的 MCP 客户端目前只支持 URL/stdio，**尚不支持自定义 HTTP header**；
  如要接进内核，需先补 `headers` / `token_env` 支持。
- 安全：使用临时账号；不要把 Key、`.env`、GitHub token 放进 box；验收结束清理文件并撤销 key。
- 记录时在模板"日期/浏览器/版本"里注明 `box / Ubuntu / Chrome <version>`，并附 VNC 截图。

> 截至 2026-11-13 官方页面口径；产品处于 beta，计费与配额可能变化。

## Box MCP 真机冒烟（2026-11-14）

在 Box by Simular 上验证「MCP 连接 + 云桌面 shell + 云 Chrome」：

- 个人 key 创建：<https://box.simular.ai/access> → Keys and devices → Create key（`cb_...`，
  只写入本地 `.env.local`，已忽略）；
- 同一 `McpToolBridge` 注册 **36 个工具**（`shell` / `read_file` / `browser_*` /
  `computer_*` / `gui_plan` / `box_*` 等）；
- `box_status`: `absent` → `box_ensure`: `running`（image `sbxdesk:v22`）；
- `shell`: `hello-from-box` · `Linux 6.8.0-142-generic x86_64` · `/workspace`；
- `browser_navigate` → `https://example.com` 返回 `Page: Example Domain`；
- 复现：`uv sync --extra mcp && uv run python examples/host_box_mcp/smoke.py`。

边界：Box Free 显示 `0 credits left`，本冒烟只用 box 的 shell/Chrome（YAI 用自己的模型）；
未做完整 Side Panel 桌面 GUI 验收；key 泄露请在 Access 页 Revoke。

## 记录模板

```text
日期/浏览器/版本：
扩展 ID：
测试页 URL：
场景编号：
审批次数：
最终状态：完成/失败/取消/unknown
页面真实变化（截图/文本）：
收据 hash：
异常与日志：
结论：通过 / 不通过 / 无法验证
```

## 通过标准

- A/B/C/D 中与本次变更相关的场景全部有证据；
- 未授权内容未进入模型上下文；
- 审批拒绝时无副作用；
- 不以“模型说完成”代替页面/工具证据；
- 未通过的场景写明原因，不标记为真机通过。
