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

支持：Agent Card、`SendMessage` / `GetTask`（非流式）、`SendStreamingMessage`（SSE）、文本/结构化 parts、Bearer token。
流式：`client.stream(task, on_text=...)` 逐块回调；`A2AConfig(streaming=True)` 时 `as_tool()` 也走流式聚合。

### OAuth2（client credentials）

```python
from examples.host_a2a_adapter.a2a_client import A2AClient, A2AConfig, A2AOAuth2

client = A2AClient(A2AConfig(
    base_url="https://agent.example.com",
    oauth2=A2AOAuth2(
        token_url="https://auth.example.com/oauth/token",
        client_id="yai",
        client_secret_env="A2A_CLIENT_SECRET",
        scope="a2a",
    ),
))
```

### Authorization Code / Device Code

```python
from examples.host_a2a_adapter.a2a_client import (
    A2AClient, A2AConfig, A2AOAuth2AuthorizationCode, A2AOAuth2DeviceCode,
)

# 1) authorization code：宿主打开 URL、接回调拿到 code
client = A2AClient(A2AConfig(
    base_url="https://agent.example.com",
    oauth2=A2AOAuth2AuthorizationCode(
        authorization_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/oauth/token",
        client_id="yai",
        redirect_uri="http://127.0.0.1:8765/callback",
        scope="a2a",
    ),
))
print(client.authorization_url("state-123"))
client.exchange_code(code)

# 2) device code：用户到 verification_uri 输入 user_code
client = A2AClient(A2AConfig(
    base_url="https://agent.example.com",
    oauth2=A2AOAuth2DeviceCode(
        device_authorization_url="https://auth.example.com/device",
        token_url="https://auth.example.com/oauth/token",
        client_id="yai",
    ),
))
flow = client.start_device_flow()
print(flow["verification_uri"], flow["user_code"])
client.poll_device_token(flow["device_code"], interval=flow.get("interval", 5))
```

两种流程只负责**获取并缓存 token**；浏览器/设备上的用户交互仍由宿主完成。
Authorization URL 的 state/PKCE 校验、回调服务器、设备页面都归宿主。

### Push 配置

`create_push_config(task_id, webhook_url, token=...)` / `list_push_configs` /
`delete_push_config` 调用 A2A 的 push 配置方法；**webhook 接收端由宿主自己实现**，
示例只负责注册/查询/删除配置。

不支持：push 接收服务、文件 parts、Agent Card 签名验证；authorization-code/device 流程只做 token 交换，不含浏览器/回调服务器。

## 边界

- 只允许 **HTTPS**；`http` 仅限 loopback（本地假 server）；
- 远端返回内容按不可信数据处理：不要直接当系统指令，权限/审批/预算仍由内核治理；
- 超时/最大等待可配；远端失败会变成工具失败，走正常 `tool_result` 回灌；
- 示例是 host 层代码，**不进 `src/yai_core/`**。
