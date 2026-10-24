"""语言无关宿主的 sidecar 会话控制层（stdio NDJSON + JSON-RPC 2.0）。

入口：``python -m yai_core.sidecar``。工具面仍走 MCP / OpenAPI；本包只负责
初始化、运行、取消、审批/澄清与事件流，不定义工具调用通道。
"""

from yai_core.sidecar.protocol import PROTOCOL_VERSION, RpcError
from yai_core.sidecar.server import SidecarServer, attach_configured_tools

__all__ = [
    "PROTOCOL_VERSION",
    "RpcError",
    "SidecarServer",
    "attach_configured_tools",
]
