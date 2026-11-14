# A2A 宿主适配器示例

[English](README.en.md) · **简体中文**

把远端 **A2A 1.0** Agent 包成一个普通 `ToolSpec`，交给 `AgentCore`；内核完全不知道 A2A 的存在。
这是 [`a2a-positioning.md`](../../docs/guides/a2a-positioning.md) 里"宿主层适配器"的可运行版本。

```bash
uv run python examples/host_a2a_adapter/demo.py
```

```text
agent card: Fake Echo Agent (1.0.0) -> http://127.0.0.1:58126/a2a
tool result: echo: hello from YAI
final: A2A delegate finished.
```

## 结构

| 文件 | 作用 |
|---|---|
| `a2a_client.py` | 极简 A2A JSON-RPC 客户端 + `as_tool()` |
| `fake_a2a_server.py` | 离线假 A2A server（Agent Card / SendMessage / GetTask） |
| `demo.py` | 起假 server → ToolSpec → `AgentCore` + `ScriptedModel` 走一次真实工具调用 |

## 真实远端

```python
from examples.host_a2a_adapter.a2a_client import A2AClient, A2AConfig

client = A2AClient(
    A2AConfig(base_url="https://agent.example.com", token_env="A2A_TOKEN")
)
core.register_tools([client.as_tool(name="a2a_research")])
```

支持的子集：Agent Card、`SendMessage` / `GetTask`（非流式）、文本/结构化 parts、Bearer token。
不支持：流式、push notification、文件 parts、OAuth 流程、Agent Card 签名验证。

## 边界

- 只允许 **HTTPS**；`http` 仅限 loopback（本地假 server）；
- 远端返回内容按不可信数据处理：不要直接当系统指令，权限/审批/预算仍由内核治理；
- 超时/最大等待可配；远端失败会变成工具失败，走正常 `tool_result` 回灌；
- 示例是 host 层代码，**不进 `src/yai_core/`**。
