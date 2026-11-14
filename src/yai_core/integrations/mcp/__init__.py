"""MCP Client 集成：把外部 MCP Server 的工具变成 Core 自己的工具。"""

from yai_core.integrations.mcp.client import (
    McpServerConfig,
    McpToolBridge,
    attach_mcp_tools,
    config_from_env,
    sanitize_schema,
)
from yai_core.integrations.mcp.registry import (
    McpRegistryClient,
    RegistryPolicy,
    RegistrySearchResult,
    RegistryServer,
)

__all__ = [
    "McpServerConfig",
    "McpToolBridge",
    "attach_mcp_tools",
    "config_from_env",
    "sanitize_schema",
    "McpRegistryClient",
    "RegistryPolicy",
    "RegistrySearchResult",
    "RegistryServer",
]
