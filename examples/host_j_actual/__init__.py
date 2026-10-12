"""host_j_actual：把 YAI Agent Core 播种进 Actual Budget（MIT 记账应用）。

见同目录 README.md。离线默认用 :class:`backend.FakeActualBackend` 完整回放
"唤醒 → 长尾对账 → 能力缺口 → 发现/委派 → 授权 → 完成" 五拍；配置好官方
``@actual-app/cli`` 与 sync server 后可无缝切到 :class:`backend.CliActualBackend`。
"""
