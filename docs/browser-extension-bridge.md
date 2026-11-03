# YAI Agent Core 浏览器扩展（MV3 · Side Panel）与 Native Bridge

> 状态（2026-11-03 本机真机验收）：**扩展已在真实 Chrome 156 / Edge 156 中加载成功**
> （`scripts/extension_smoke.py`：`--pack-extension` 通过、MV3 service worker 出现在 CDP targets）；
> **Bridge 已与真实 Sidecar 端到端验证**（`scripts/bridge_smoke.py`）。
> 仍未验证：侧边栏点击流、Native Messaging 注册（需写注册表）、安装包分发与 Service Worker 回收。
> 这些必须按文末清单人工验收，不能把"加载成功"当成"交互链路已验证"。
## 结构

```text
Chrome / Edge Extension (MV3)
  ├─ Service Worker：只做侧边栏 ↔ Bridge 的连接/转发，随时可被浏览器回收
  ├─ Side Panel：任务输入、事件流、审批/澄清、取消、连接状态
  └─ chrome.scripting（activeTab）：用户点击"读取当前页面"才注入一次性采集函数
        ⇅ Native Messaging（4 字节小端长度 + UTF-8 JSON）
  yai_core.sidecar.native_bridge（本地 Bridge 进程）
        ⇅ stdio NDJSON + JSON-RPC 2.0（不修改既有 Sidecar 公开协议）
  yai_core.sidecar → AgentCore
        ├─ ToolExecutor / PermissionPolicy / 审批 / 执行收据
        ├─ Outcome Verification（宿主 verifier 可选）
        └─ Browser Provider（可选，Playwright）
```

## 安全边界（按任务书 §5.3）

| 项 | 做法 |
|---|---|
| 扩展身份 | Bridge 只认 Chrome 传入的 `chrome-extension://<id>/`，且必须在本地配置 `allowed_origins` 内；空配置 = 拒绝所有，fail closed |
| 凭据 | **浏览器端不保管任何高权限凭据**；模型 / 策略等 `initialize` 参数只来自本地 Bridge 配置文件 |
| 操作白名单 | 扩展只能发 `hello / run / cancel / host_response / ping / status / shutdown`；未知操作直接拒绝 |
| 字段白名单 | 扩展消息里没有"传模型配置""传 API Key"的入口；`host_response` 只允许回应 `srv-*` 反向请求 |
| 大小限制 | 入站帧 ≤ 1 MiB；出站超限替换为 `truncated` 标记（避免 Chrome 杀 host 进程） |
| 读取页面 | 默认不注入脚本；用户点击"读取当前页面"才用 `activeTab` + `chrome.scripting` 一次性采集，页面内容按**不可信输入**附加到任务 |

## 安装（Windows / Chrome 或 Edge）

1. 安装 YAI（仓库根目录）：`uv sync --extra llm`（真实模型）或按需加 `--extra browser`。
2. 构建扩展：Chrome → `chrome://extensions` → 打开"开发者模式" → "加载已解压的扩展程序" → 选本仓库 `extension/` 目录。记下扩展 ID。
3. 准备 Bridge 配置：复制 `extension/bridge.config.example.json` 到仓库外（例如 `%LOCALAPPDATA%\YAI\bridge.json`），把 `allowed_origins` 换成 `chrome-extension://<扩展 ID>/`。
4. 生成可执行的 Native Host：Chrome 的 manifest `path` 必须指向可执行文件，
   `python.exe` 无法携带 `-m` 参数。推荐二选一：
   - **PyInstaller（推荐）**：`pyinstaller --onefile --name yai-bridge extension/native/bridge_host.py`，
     把 `com.yai.agentcore.json` 的 `path` 指向生成的 `yai-bridge.exe`；
   - 开发机调试：`python extension/native/bridge_host.py chrome-extension://<扩展 ID>/`（用配置环境变量）。
   填写 `extension/native/com.yai.agentcore.json` 的 `path` 与 `allowed_origins`（换成扩展 ID）。
5. 注册 Native Messaging Host（Chrome，当前用户）：
   ```powershell
   New-Item -Path 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.yai.agentcore' -Force | Out-Null
   Set-ItemProperty -Path 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.yai.agentcore' `
     -Name '(default)' -Value 'C:\path\to\com.yai.agentcore.json'
   ```
   Edge 把 `Google\Chrome` 换成 `Microsoft\Edge`。Linux / macOS 路径见
   [Chrome 官方文档](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)。
6. 点扩展图标打开侧边栏；状态徽标显示"已连接本地 Sidecar"后即可运行任务。

> 模型凭据：在启动 Chrome 的环境里设置 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `LLM_MODEL`
> （Sidecar 默认模型工厂读取这些变量），**不要**写进扩展或浏览器消息。

## 协议映射

| 浏览器消息 | Sidecar（JSON-RPC） | 说明 |
|---|---|---|
| `hello {protocol:1}` | — | 版本协商；不匹配回 `hello_rejected` |
| `run {task}` | 请求 `agent/run` | 回 `run_started {run_id}`；同一时刻只允许一个 run |
| `cancel {run_id}` | 请求 `agent/cancel` | 回 `run_cancel_requested` |
| `host_response {request_id,result}` | 对 `srv-*` 的响应 | 仅接受反向请求 ID；其余拒绝 |
| `ping` / `status` / `shutdown` | `ping` / — / `shutdown` | 状态与收尾 |
| — | 通知 `agent/event` 等 | 转成 `{type:"notification", method, params}` |
| — | 请求 `approval/request` / `clarify/request` | 转成 `{type:"host_request"}`，由侧边栏弹确认/追问 |

## 生命周期与已知限制（诚实声明）

- Bridge 是**与一次扩展连接绑定的短期进程**：连接断开 → Sidecar 结束，**不提供跨进程任务恢复**。
- 断连 / Service Worker 被回收后，侧边栏把结果标为 **unknown**，**不会自动重做**可能已有副作用的操作；
  用户需要重新描述任务或先确认外部状态。
- 当前只支持 Chrome / Edge（Chromium 116+ Side Panel）；Firefox 等未适配。
- **已真机验证**：扩展在 Chrome 156 / Edge 156 中能被 `--load-extension` 加载并注册
  MV3 service worker（`scripts/extension_smoke.py`，以 CDP target 为证据）；Bridge 与
  真实 Sidecar 的 hello / status / shutdown 通过（`scripts/bridge_smoke.py`）。
- **仍未真机验证**：侧边栏点击流（工具栏图标 + 用户手势）、Native Messaging 注册
  （需要写注册表）、安装包分发、Service Worker 回收后的重连，以及跨浏览器差异。

## 本机验收命令（2026-11-03）

```powershell
# 1) 真实浏览器任务（Playwright Chromium；也可 --channel chrome 用系统 Chrome）
.\.venv\Scripts\python.exe scripts\browser_smoke.py --json

# 2) Bridge ↔ 真实 Sidecar（无浏览器，帧协议 + 未授权来源拒绝）
.\.venv\Scripts\python.exe scripts\bridge_smoke.py --json

# 3) 扩展真机加载（有头 Chrome / Edge；headless 不会加载扩展）
.\.venv\Scripts\python.exe scripts\extension_smoke.py --json
.\.venv\Scripts\python.exe scripts\extension_smoke.py --browser msedge --json

# 4) Windows 桌面 UIA 冒烟（自建窗口，不碰用户程序）
.\.venv\Scripts\python.exe scripts\windows_uia_smoke.py --json
```

证据摘要：浏览器任务 11 条执行收据（来源闸拦截、旧引用过期均验证）；Chrome / Edge 两
端各自出现 `chrome-extension://<id>/service_worker.js` target；Bridge 未授权来源退出码 2、
授权来源握手/状态/关闭全通过。注意 **未加载 unpacked 扩展的 ID 由路径决定**，正式注册
native host 时要用固定安装路径下的稳定 ID。

## 验证清单（需要浏览器环境）

1. `chrome://extensions` 加载无错误；点图标打开侧边栏。
2. 状态从"连接中"变为"已连接本地 Sidecar"。
3. 输入任务 → 事件流出现 `strategy_selected / tool_call / tool_result / done`。
4. 审批工具时侧边栏出现确认卡，允许/拒绝都传回 Sidecar。
5. 运行中点"取消"，Sidecar 发 `agent/cancelled`。
6. 关闭浏览器（或强杀 Bridge）后重开：状态为 unknown，不自动重发任务。
7. 未授权扩展 ID 的 Bridge 直接拒绝启动（退出码 2）。
