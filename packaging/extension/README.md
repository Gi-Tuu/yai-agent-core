# 固定扩展 ID（Chrome / Edge）

开发时直接加载 `extension/` 的 ID 由**路径**决定，换机器/目录会变，导致
Native Host 的 `allowed_origins` 与注册表失配。正式分发必须固定 ID。

## 生成密钥并构建固定 ID 副本

```powershell
.\.venv\Scripts\python.exe packaging\extension\keygen.py
.\.venv\Scripts\python.exe packaging\extension\build_extension.py
```

- `keygen.py` 生成 `packaging/extension/extension-key.pem`（**私钥，gitignore，离线保管**）；
- `build_extension.py` 把公钥写入 `dist/extension-<version>/manifest.json` 的 `key`，
  并输出 `dist/extension-id.txt`；
- 32 位扩展 ID 由公钥 SHA-256 前 128 位映射到 a–p，之后换机器/目录都稳定。

## 安装时使用固定 ID

```powershell
.\.venv\Scripts\python.exe packaging\windows\installer.py `
  --extension-id (Get-Content dist\extension-id.txt) `
  --with-browser-mcp --cdp-endpoint http://127.0.0.1:9222
```

在 `chrome://extensions` / `edge://extensions` 加载 `dist/extension-<version>/`。
**注意**：从旧的路径 ID 切到固定 ID 会改变扩展 ID，旧 `bridge.json` 的
`allowed_origins` 与注册表必须重新安装；Key/模型配置会被安装器保留。

## 私钥纪律

- 私钥只用于打包/商店签名，不提交、不写进扩展、不通过面板传输；
- 丢失私钥 = 无法继续用同一个固定 ID 发布，需要重新生成并迁移用户；
- CI 不持有私钥；发布由人工离线完成。
