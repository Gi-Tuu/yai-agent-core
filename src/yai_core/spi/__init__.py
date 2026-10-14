"""SPI（Service Provider Interface）：宿主可替换、Core 提供默认实现的契约层。"""

from yai_core.spi.channel import Channel
from yai_core.spi.cipher import StorageCipher
from yai_core.spi.discovery import ToolDiscovery
from yai_core.spi.embedding import EmbeddingProvider
from yai_core.spi.learning import RouteSelector
from yai_core.spi.memory import MemoryStore
from yai_core.spi.model import ModelProvider
from yai_core.spi.policy import PermissionDecision, PermissionPolicy
from yai_core.spi.resources import (
    PathEscapeError,
    ResourceConflictError,
    ResourceContent,
    ResourceEntry,
    ResourceError,
    ResourceNotFoundError,
    ResourcePolicy,
    ResourceProvider,
    ResourceTooLargeError,
    normalize_relative,
    resolve_within_root,
    sha256_text,
)
from yai_core.spi.sandbox import SandboxResult, ToolSandbox

__all__ = [
    "Channel",
    "StorageCipher",
    "EmbeddingProvider",
    "MemoryStore",
    "ModelProvider",
    "PermissionDecision",
    "PermissionPolicy",
    "ToolDiscovery",
    "RouteSelector",
    "ToolSandbox",
    "SandboxResult",
    "ResourceProvider",
    "ResourcePolicy",
    "ResourceEntry",
    "ResourceContent",
    "ResourceError",
    "PathEscapeError",
    "ResourceNotFoundError",
    "ResourceTooLargeError",
    "ResourceConflictError",
    "normalize_relative",
    "resolve_within_root",
    "sha256_text",
]
