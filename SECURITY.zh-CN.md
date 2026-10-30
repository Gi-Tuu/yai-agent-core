# 安全策略

[English](SECURITY.md) · **简体中文**

## 支持版本

| 版本 | 安全更新 |
| --- | --- |
| 0.24.x | ✅ |
| 0.23.x | ✅ |
| < 0.23 | ❌ |

> 安全修复只在最新 minor（0.24.x）发布；历史版本不再 backport——上表中的 ✅ 仅表示该版本仍处在支持窗口内。

## 部署安全默认（暴露到公网前务必阅读）

1. **必须配置 API Key 鉴权**：FastAPI 服务在未设置 `YAI_API_KEY`（或 `YAI_API_KEYS`）
   时，鉴权处于**关闭状态**——这是为本地/演示环境提供的零配置体验。公网部署若不设置，
   任何人都可调用你的接口并消耗模型额度。请通过安全渠道设置
   `YAI_API_KEY=<随机长字符串>`，请求时携带 `Authorization: Bearer <key>`。
2. **代码工具沙箱边界**：
   - `SubprocessSandbox` 为演示级隔离（限制内置函数、CPU/内存、超时），
     **不做网络隔离、不保证抵御蓄意逃逸**，只适合运行纯计算代码；
   - 暴露到公网或处理不可信输入时，请使用 `DockerSandbox`（无网络、只读根文件系统、
     drop ALL capabilities、内存/PID 限制），或用 gVisor / Firecracker 实现同一
     `ToolSandbox` 契约。
3. **发现不等于授权**：动态发现（MCP / OpenAPI）的工具注册后，执行仍统一经过
   包含 `PermissionPolicy` 在内的授权检查。默认 Core 策略放行；宿主必须显式配置
   `policy` 或声明 `approval.required`，才能要求高危操作（写入 / 网络 / shell）经批准。
   必需审批不能绕过策略拒绝，动态发现也不能绕过统一授权。
4. **密钥与数据**：`.env` 已在 `.gitignore`，请勿提交真实 API Key；SQLite 访问全部
   使用参数化查询，不拼接 SQL。

## 报告漏洞

若发现安全问题，请**不要**公开提 issue，可通过以下方式私密报告：

- GitHub：仓库 → Security → Report a vulnerability；
- 邮箱：19252270588@163.com（请附复现步骤、影响范围与修复建议）。

确认后我们会尽快回复，并在修复发布前保持细节不公开。
