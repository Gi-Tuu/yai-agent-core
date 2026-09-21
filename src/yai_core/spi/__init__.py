"""SPI（Service Provider Interface）：宿主可替换、Core 提供默认实现的契约层。"""

from yai_core.spi.channel import Channel
from yai_core.spi.discovery import ToolDiscovery
from yai_core.spi.memory import MemoryStore
from yai_core.spi.model import ModelProvider
from yai_core.spi.policy import PermissionDecision, PermissionPolicy
from yai_core.spi.sandbox import SandboxResult, ToolSandbox

__all__ = [
    "Channel",
    "MemoryStore",
    "ModelProvider",
    "PermissionDecision",
    "PermissionPolicy",
    "ToolDiscovery",
    "ToolSandbox",
    "SandboxResult",
]
