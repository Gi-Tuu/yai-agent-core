# Windows 安装与分发（v0.27 Batch 6）

这是**便携包 + 一键安装脚本**，不是 MSI 安装程序，也不包含 Python/Chrome/模型。

## 组件

| 文件 | 作用 |
|---|---|
| `installer.py` | 复制 Bridge 到 `%LOCALAPPDATA%\YAI\bin\yai-bridge.exe`，调用 `scripts/register_native_host.py` 写配置/manifest 并注册 Chrome/Edge HKCU |
| `install.cmd` / `uninstall.cmd` | 双击/命令行包装（优先使用仓库 `.venv` 的 Python） |
| `build_bundle.py` | 构建 `dist/windows-bundle/` 与 `dist/yai-agent-core-windows-<version>.zip`（Bridge + 扩展 + 脚本 + 本说明） |

## 使用

```powershell
# 从源码仓库安装（使用最新 dist\yai-bridge-v5.exe）
.\.venv\Scripts\python.exe packaging\windows\installer.py `
  --extension-id <32位扩展ID> --with-browser-mcp --cdp-endpoint http://127.0.0.1:9222

# 或双击 install.cmd（参数同上）

# 卸载（默认保留 bridge.json，含模型配置）
.\.venv\Scripts\python.exe packaging\windows\installer.py --action uninstall
# 连配置/Key 一起删：
... --action uninstall --purge-config
```

扩展本体仍需在 `chrome://extensions` / `edge://extensions` 手动“加载已解压的扩展
程序”（选择仓库 `extension/` 目录；便携包中为 `extension/`）。

构建便携包：

```powershell
.\.venv\Scripts\python.exe packaging\windows\build_bundle.py
```

产物（gitignore）：`dist/windows-bundle/`、`dist/yai-agent-core-windows-0.26.0.zip`。

## 真机验收（2026-11-03，Windows 11）

| 项 | 结果 |
|---|---|
| 安装：复制 Bridge + 注册 Chrome/Edge | 通过（`%LOCALAPPDATA%\YAI\bin\yai-bridge.exe`、manifest、`install.json`） |
| 启动/重装后侧边栏握手 | 通过（Bridge v1 协议握手完成，模型/MCP 状态） |
| 卸载：注销 Chrome/Edge、删 Bridge | 通过（`removed_registry: chrome, edge`，`bridge.json` 保留） |
| 重新安装恢复 | 通过 |
| 便携包构建 | `dist/yai-agent-core-windows-0.26.0.zip`（约 46.6 MB，含冻结 Bridge） |
| 便携包内 installer dry-run | 通过 |

## 边界（诚实声明）

- **不是 MSI，也不是 Chrome Web Store / Edge Add-ons 分发**；扩展仍需手动加载。
- 目标机需要 **Python 3.11+** 且已安装 `yai-agent-core`（或使用源码仓库 venv）；
  便携包不打包 Python 运行时与模型。
- 不做自动更新、回滚、静默安装；卸载默认保留 `bridge.json`，`--purge-config` 才删 Key。
- 安装器只写 HKCU，不需要管理员权限；浏览器内扩展本体需用户手动移除。
- 真实鼠标点击工具栏图标仍建议人工验收一次（安装器无法代替）。
