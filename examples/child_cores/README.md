# 可安装子核示例（Phase 3 生态）

两个"子产品"示例，每个都自带独立 `AgentCore` + 本机 IPC 服务，可单独运行，也可被父核托管：

| 子核 | 文件 | 工具 |
|---|---|---|
| 笔记应用 | `notes_app.py` | `add_note` / `search_notes` |
| 计算器应用 | `calc_app.py` | `add` / `multiply` / `history` |

## 单独运行（子核独立可用）

```bash
python examples/child_cores/notes_app.py --discovery-dir ./.tmp/fed --parent-id hub
python examples/child_cores/calc_app.py  --discovery-dir ./.tmp/fed --parent-id hub
```

各自写一个发现文件（`<discovery-dir>/<node-id>.json`），父核凭它连接。

## 父核托管两个子核（离线 demo）

```bash
python examples/child_cores/hub_demo.py
```

流程：父核 `ManagedChild` 拉起两个子进程 → 握手拿到各自工具 → 注册为
`notes__*` / `calc__*` → 模型依次调用 → 读回子核状态 → 停止子进程。

## 安全与边界

- 只绑定 `127.0.0.1`；发现文件含一次性 token（POSIX 0600）；
- 子核有独立策略与数据，父核只能调用它声明的工具；
- 真实生态里子核由各自产品进程拉起；此目录是"两个产品 + 一个父核"的最小可跑形态。
