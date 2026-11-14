# host_o_go_mcp · Go 宿主经 MCP 接入

[English](README.en.md) · **简体中文**

一个**零第三方依赖**的 Go stdio MCP Server（`initialize` / `tools/list` / `tools/call`），
由 Python 侧 `AgentCore` 通过 MCP Client 拉起并调用 `lookup_order`。

```bash
# 需要 Go 1.22+；在仓库根运行
uv run python examples/host_o_go_mcp/run.py
```

没有 Go 时脚本会打印安装指引并退出码 **2**（不会尝试联网安装）。

## 结构

| 文件 | 作用 |
|---|---|
| `go.mod` / `main.go` | 最小 Go MCP Server：新增行分隔 JSON-RPC，提供 `lookup_order` |
| `run.py` | 检查 Go → `McpToolBridge` 拉起 `go run` → 注册工具 → ScriptedModel 调一次 |

## MCP 边界

- Go Server 只暴露 MCP 工具面；工具在 YAI 里仍是普通 `ToolSpec(source="mcp")`；
- 协议里没有新的工具调用通道，Python 侧的路由/权限/审计全部照旧；
- 这是**示例**，不加入 CI 必跑矩阵（Go 不是项目依赖）。

## 已知边界

- 当前机器未安装 Go，Go 代码未经本机编译验证；`run.py` 的无 Go 分支已由离线测试覆盖；
- Go Server 只实现示例所需的最小方法集，不追求完整 MCP 兼容。
