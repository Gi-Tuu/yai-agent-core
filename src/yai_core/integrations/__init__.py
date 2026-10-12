"""可选集成层：把外部生态能力接入内核，全部懒加载、零硬依赖。

- integrations.mcp：MCP Client，把外部 MCP Server 的工具注册进 ToolRegistry。
- integrations.openapi：OpenAPI 发现，把任意 OpenAPI 3 REST API 自动注册为工具。
- integrations.embedding：OpenAI 兼容文本嵌入后端，为语义发现提供向量。
- integrations.tracing：OpenTelemetry 结构化 tracing（可选 otel extra），在外侧订阅
  事件流，把一次 run 转成一条 trace，不侵入 kernel。
"""
