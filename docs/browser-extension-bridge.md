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
3. 生成可执行的 Native Host（Chrome 的 manifest `path` 必须指向可执行文件，
   `python.exe` 无法携带 `-m` 参数）：
   ```powershell
   .\.venv\Scripts\python.exe -m PyInstaller --onefile --name yai-bridge `
     --distpath dist --workpath build\pyinstaller --specpath build\pyinstaller `
     --paths src extension\native\bridge_host.py
   ```
4. 注册（一条命令写配置 + manifest + Chrome/Edge 的 HKCU 注册表，不需要管理员权限；`--dry-run` 可先预览）：
   ```powershell
   .\.venv\Scripts\python.exe scripts\register_native_host.py `
     --extension-id <你的扩展ID> --launcher dist\yai-bridge.exe --python .\.venv\Scripts\python.exe
   ```
   它会写 `%LOCALAPPDATA%\YAI\bridge.json`（`allowed_origins` + `sidecar_command`，**不含模型凭据**）
   与 `%LOCALAPPDATA%\YAI\com.yai.agentcore.json`，并注册
   `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.yai.agentcore` 与
   `HKCU\Software\Microsoft\Edge\NativeMessagingHosts\com.yai.agentcore`。
   卸载：`python scripts\register_native_host.py --unregister`。
5. 点扩展图标打开侧边栏；状态徽标显示"已连接本地 Sidecar"（或"协议握手完成"）后即可运行任务。
   排障日志：`%LOCALAPPDATA%\YAI\bridge.log`（Chrome 看不到 native host 的 stderr，
   Bridge 会把启动/错误写在这里）。

> 模型凭据：在启动 Chrome 的环境里设置 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `LLM_MODEL`
> （Sidecar 默认模型工厂读取这些变量），**不要**写进扩展或浏览器消息。改完环境变量后
> 需要重启 Chrome（子进程继承启动时的环境）。

## 模型与工具接入（本机实测）

- **模型凭据**：`register_native_host.py --env-file .env` 会把 `OPENAI_API_KEY / OPENAI_BASE_URL /
  LLM_MODEL / LLM_STRONG_MODEL` 写进本地 `%LOCALAPPDATA%\YAI\bridge.json` 的
  `initialize.model`（**只在本机配置文件里，不进入扩展、不发给浏览器**；打印结果时不会回显 Key）。
  不想落盘的话，也可以改用用户级环境变量 + 重启浏览器。
- **浏览器能力（可选）**：`--browser-mcp playwright` 会在 `initialize.mcp` 里加一个
  Playwright MCP server（`npx -y @playwright/mcp@latest --browser chrome`，默认有头、用户能看到
  被操作的 Chrome；`--mcp-headless` 可无头）。这是**可选扩展**：MCP 走既有 `[mcp]` extra
  与懒加载集成，内核 `dependencies` 仍为空（AST 守门测试照常通过）。
- **审批**：默认 `policy.mode=auto`，白名单外工具会在侧边栏弹确认卡；`allow_all` 仅建议本机演示。

本机端到端实测（真实 Edge + 真实 Chrome via Playwright MCP）：任务「打开百度首页，告诉我标题」
→ 路由 `react` → 两次审批 → `browser_navigate` / `browser_evaluate` 成功 → 最终回答
「百度一下，你就知道」。截图：`docs/exports/extension-e2e.png`（gitignore）。

## CDP 模式：让工具操作你自己的 Chrome 窗口

默认的 Playwright MCP 会新开一个受控浏览器；想操作用户自己的 Chrome 会话，用 CDP 连接：

```powershell
# 1) 启动一个带远程调试端口的 Chrome（独立 profile；Chrome 不允许对默认 profile 开调试端口）
.\.venv\Scripts\python.exe scripts\chrome_cdp.py start          # 默认 http://127.0.0.1:9222
.\.venv\Scripts\python.exe scripts\chrome_cdp.py status

# 2) 把 MCP 配置改成 CDP 端点（会写进本地 bridge.json）
.\.venv\Scripts\python.exe scripts
egister_native_host.py `
  --extension-id <扩展ID> --launcher dist\yai-bridge-v2.exe --python .\.venv\Scripts\python.exe `
  --env-file .env --browser-mcp playwright --cdp-endpoint http://127.0.0.1:9222

# 3) 关闭再打开侧边栏让 Bridge 重新加载配置
# 4) 用完停止调试用 Chrome（不影响你平时用的 Chrome）
.\.venv\Scripts\python.exe scripts\chrome_cdp.py stop
```

**本机真机验证（真实 Edge + 真实 Chrome/156 via CDP）**：侧边栏显示
`协议握手完成（bridge v1 · 模型 deepseek/deepseek-v4.1-flash-1017 · MCP browser）`；
任务「打开百度首页，告诉我标题」→ 两次审批 → `browser_navigate` / `browser_evaluate`
成功，回答「百度一下，你就知道」；`http://127.0.0.1:9222/json/list` 中当前页面为
`https://www.baidu.com/`。截图：`docs/exports/extension-cdp-e2e.png`（gitignore）。

> 说明：CDP 连接的是 `scripts/chrome_cdp.py` 启动的那个 Chrome（独立 profile），
> 不是你正在用的默认 Chrome 窗口；已有 Chrome 无法事后开启调试端口。若想接入已有窗口，
> 需要以 `--remote-debugging-port` 启动它。

## LLM 配置入口（面板 + 本地 CLI）

侧边栏里有 **“LLM 设置（保存到本机配置）”** 折叠区：可填 Base URL / 模型 / 强模型 /
API Key（密码框）。行为：

- “读取当前配置”会回填模型/Base URL，Key 只显示 `***末4位`（Bridge 永远不回传明文）；
- “保存到本机”把配置写进 `%LOCALAPPDATA%\YAI\bridge.json`（先备份 `.bak`），
  **关闭再打开侧边栏生效**（Bridge 重新读取配置）；
- Key 只在点击保存时经一次内存传输，写入本机配置文件；**不存上次/不写 localStorage**，
  输入框保存后清空；面板不提供“读回明文 Key”。
- 本机实测：读取后输入框回填 `https://api.suanli.cn/v1` / `deepseek/...`，
  Key 显示 `已配置（***QRhb）`；保存返回“已保存到本机配置；关闭再打开侧边栏后生效”。
  截图：`docs/exports/extension-llm-settings.png`（gitignore）。

仍保留等价 CLI（适合脚本/CI）：

```powershell
.\.venv\Scripts\python.exe scripts\configure_bridge_llm.py --show
Get-Content .env | .\.venv\Scripts\python.exe scripts\configure_bridge_llm.py --api-key-stdin
```

## LLM 配置（本地，不走浏览器）

面板只显示当前模型 / MCP 名字（`hello_ok` 的非敏感字段）；API Key 不进浏览器：

```powershell
.\.venv\Scripts\python.exe scripts\configure_bridge_llm.py --show
.\.venv\Scripts\python.exe scripts\configure_bridge_llm.py --model deepseek/deepseek-v4.1-flash-1017 `
  --base-url https://api.suanli.cn/v1
Get-Content .env | .\.venv\Scripts\python.exe scripts\configure_bridge_llm.py --api-key-stdin  # Key 不落命令历史
```

修改后关闭/重开侧边栏生效；脚本输出永远对 Key 脱敏。MCP 与这些配置都是宿主侧可选能力，
内核 `dependencies = []` 不变。

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
  真实 Sidecar 的 hello / status / shutdown 通过（`scripts/bridge_smoke.py`）；
  **Native Messaging 端到端已在真实 Edge 验证**：注册后侧边栏状态变为
  "协议握手完成（bridge v1）/ 就绪"，`%LOCALAPPDATA%\YAI\bridge.log` 记录
  `frozen=True` 启动与 `exit ok`。
- **排障**：面板显示 "Error when communicating with the native messaging host" 时，
  先看 `%LOCALAPPDATA%\YAI\bridge.log`。实测 Chrome/Edge 会额外传
  `--parent-window=0`（已处理，不再被当成配置路径）；也检查 manifest 的
  `allowed_origins` 是否与扩展 ID 完全一致。
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

## 看不到侧边栏？按这个顺序排查

1. **要点扩展图标，不是浏览器自带的"侧边栏"按钮**：`chrome://extensions` 打开开发者模式 →
   加载已解压的扩展程序 → 选 `extension/`；然后在工具栏把 YAI 图标**固定**，点击它 →
   浏览器右侧出现 Side Panel。扩展不会在加载后自动弹出侧边栏。
2. **改过代码要重新加载**：在 `chrome://extensions` 的 YAI 卡片上点"重新加载"（或移除后
   重新加载），否则浏览器里跑的还是旧版 service worker。
3. **仍无反应时**：打开 `chrome://extensions` → YAI 卡片 → 点 "Service Worker" 链接查看
   console 错误；也可以直接在新标签页打开
   `chrome-extension://<你的扩展 ID>/sidepanel.html`——这是同一套 UI 的标签页版本，
   用来确认面板本身能渲染、错误原因是什么。
4. **状态显示"未注册本地 Bridge"是正常的**：侧边栏已打开，只是还没按第 3 节注册
   Native Messaging Host；注册前点"运行"会得到明确错误，而不是静默失败。
5. **新版 Chrome 对命令行 `--load-extension` 有限制**：这不影响你在 UI 里手动加载；
   自动化验收脚本用 Playwright 自带 Chromium / Edge 覆盖加载、侧边栏页面渲染与
   `openPanelOnActionClick` 行为。

> 扩展已内置兜底：service worker 顶层与 onInstalled/onStartup 都会设置
> `openPanelOnActionClick`；点击图标时若 `chrome.sidePanel.open` 不可用或失败，
> 会自动把 `sidepanel.html` 开成普通标签页，避免"点了没反应"。

## 验证清单（需要浏览器环境）

1. `chrome://extensions` 加载无错误；点图标打开侧边栏。
2. 状态从"连接中"变为"已连接本地 Sidecar"。
3. 输入任务 → 事件流出现 `strategy_selected / tool_call / tool_result / done`。
4. 审批工具时侧边栏出现确认卡，允许/拒绝都传回 Sidecar。
5. 运行中点"取消"，Sidecar 发 `agent/cancelled`。
6. 关闭浏览器（或强杀 Bridge）后重开：状态为 unknown，不自动重发任务。
7. 未授权扩展 ID 的 Bridge 直接拒绝启动（退出码 2）。
