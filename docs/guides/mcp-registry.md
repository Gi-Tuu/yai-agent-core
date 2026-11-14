# MCP Registry 发现（只读）

[English](mcp-registry.en.md) · **简体中文**

[官方 MCP Registry](https://registry.modelcontextprotocol.io/) 是 MCP Server 的公共元数据目录。
YAI 提供**只读发现**：按关键词搜索、查看 server 的 remote/package 信息、再由宿主**显式**决定是否接入。
它不自动安装包、不执行 registry 里的命令、也不自动连接任何 server。

## 1. 快速使用

```bash
uv run python examples/host_mcp_registry/search.py sql --limit 3
uv run python examples/host_mcp_registry/search.py github --json
```

输出示例：

```text
ai.getvda/elasticsearch-redis-mysql-stack-generator  (1.0.0) latest
  Elasticsearch + Redis + MySQL Stack Generator
  remote[streamable-http] https://.../mcp  auth-headers=-
```

## 2. 代码入口

```python
from yai_core.integrations.mcp import McpRegistryClient

result = McpRegistryClient().search("sql", limit=5)
for server in result.servers:
    print(server.name, server.version, server.safe_description)
    for remote in server.remotes:
        print(remote.type, remote.url, remote.required_headers)

# 只有在你确认信任该 server、并补齐变量/头之后，才显式构建配置（不发网络请求）
config = result.servers[0].build_config(
    alias="registry_demo",
    variables={"tenant": "eu"},
    headers={"X-API-Key": "..."},   # 实际应从环境/密钥库取
)
```

## 3. 安全边界（重要）

Registry 是**不可信输入**，接入前至少检查：

- **来源**：优先官方 namespace / 有源码仓库的 server；`repository_url` 只是声明，不等于已验证；
- **传输**：只接受 `https` + `streamable-http`；URL 模板必须显式补全，不猜默认值；
- **鉴权**：`isSecret` 的头由宿主提供（环境变量 / 密钥库），不要写进代码或提交；
- **描述投毒**：server 的标题/描述会经 `sanitize_untrusted_text` 去控制/零宽/双向字符并截断，
  但**不做语义指令检测**；高风险环境用 `trust_tool_descriptions=False`（见 MCP 安全一节）；
- **版本**：同一 server 可能有多个版本，接入时建议固定 `version`；
- **权限**：接入后仍走内核 `PermissionPolicy` / 审批 / 审计，不因"来自 registry"而放宽。

YAI 不替你判断"这个 server 是否可信"；它只把元数据整理好，**信任决策在宿主**。

## 4. 边界

- 只读搜索（`GET /v0/servers`），不发布/不修改 registry；
- 不做 OAuth 流程、不自动安装 npm/pypi/oci 包、不执行 registry 里的命令；
- Registry API 仍在 v0，字段可能变化；解析层对缺失字段容错，但新字段会被忽略；
- 只支持 `streamable-http` remote 转 `McpServerConfig`；`sse` remote 仅展示，不自动接入。

## 5. 测试

```bash
uv run pytest tests/test_mcp_registry.py -q
```

覆盖：嵌套响应解析、隐藏字符净化、latest 标记、URL 变量/必填头、拒绝 HTTP 与未解析模板、
畸形响应报错；全部离线（`urlopen` 注入假响应）。
