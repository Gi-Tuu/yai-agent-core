# ADR 0007：本地模型 Key 的存储边界与 SecretStore 方案

[English](0007-local-secret-storage.en.md) · **简体中文**

- 状态：Proposed（2026-11-03，v0.27 Batch 4）
- 关联：`src/yai_core/sidecar/native_bridge.py`、`scripts/register_native_host.py`、
  `scripts/configure_bridge_llm.py`、`docs/guides/browser-extension-bridge.md`
- 关联任务书：v0.27 §8（本地 API Key 存储安全）

## 背景

当前模型 Key 存在本机 `%LOCALAPPDATA%\YAI\bridge.json` 的
`initialize.model.api_key`：

- 不进入浏览器存储；面板读回永远脱敏（`***末4位`）；
- 配置写入有跨进程锁 + 原子替换 + `.bak` 备份；
- **但明文落盘**：脱敏显示不是磁盘加密。同用户进程、备份文件、误上传的
  `bridge.json.bak`、以及具备该用户目录读权限的程序都可以读到 Key。

因此不能对外宣传"Key 已加密存储"。本 ADR 记录跨平台方案与当前边界。

## 决策

1. **短期（v0.27）不引入 OS 专属依赖到 Core**，但把风险说清楚：
   - 文档明确 `bridge.json` / `.bak` 是明文敏感文件、位置与 ACL 责任；
   - 日志/异常/脚本输出继续保证不打印 Key（已有测试）；
   - 卸载/轮换文档化：删除配置与 `.bak`，或覆盖为新 Key。
2. **中期新增可选 `SecretStore` SPI**（不进入内核硬依赖），Candidates：
   - Windows：DPAPI `CryptProtectData`（当前用户绑定）或 Credential Manager
     （`wincred`）；都以可选模块/extra 提供，Core 只依赖 Protocol。
   - Linux：Secret Service（libsecret）/ 文件权限 + 显式警告。
   - macOS：Keychain。
   - 统一接口（草案）：
     ```python
     class SecretStore(Protocol):
         async def get(self, key: str) -> str | None: ...
         async def set(self, key: str, value: str) -> None: ...
         async def delete(self, key: str) -> None: ...
     ```
3. **Bridge 配置迁移**：未来 `bridge.json` 只存 provider/base_url/model 等非敏感字段；
   `api_key` 改为 `secret_ref`（如 `dpapi:model_api_key`），由 Bridge 启动时经
   `SecretStore` 解析。旧配置自动迁移（一次性写 SecretStore、从文件移除 Key、保留
   `.bak` 但不含 Key 的新备份），迁移失败则保持旧格式并明确告警，绝不丢 Key。
4. **兼容**：现有 `configure_llm`/CLI 输入 Key 的 API 不变；内部写入路径可切换为
   SecretStore；读回仍是脱敏。

## 当前风险（必须如实告知用户）

| 风险 | 状态 |
|---|---|
| 磁盘明文 Key（bridge.json） | 存在；文件权限依赖用户目录 ACL |
| `.bak` 备份含旧 Key | 存在；轮换后旧 Key 仍可能在 `.bak` |
| 日志/异常泄漏 Key | 已用脱敏与测试守住；本 ADR 不声称覆盖所有未来日志 |
| 多用户/共享机器 | 不支持安全隔离；每个 OS 用户独立安装/配置 |

## 验收（未来实现 SecretStore 时）

- Windows DPAPI 往返、错误密钥/跨用户解密失败、删除/轮换；
- bridge.json 不再含明文 Key（含 `.bak` 迁移后）；
- 无 SecretStore 时回退旧格式并发出明确 warning，不静默降级；
- 所有测试离线，不依赖真实云端 Key。

## 被否决/暂缓

- **直接把 DPAPI 塞进 Core**：破坏零第三方硬依赖与跨平台；暂缓为可选模块。
- **自研加密并把密钥放同目录**：等价于明文，不采纳。
