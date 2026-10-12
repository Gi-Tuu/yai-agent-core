"""YAI Agent Core —— 进程内嵌入式自适应 Agent 内核。

宿主软件只声明能力，Core 自动发现、自适应规划与执行。
"""

from yai_core.config import CoreConfig
from yai_core.core import AgentCore
from yai_core.diagnostics import (
    DiagnosticEvent,
    DiagnosticKind,
    DiagnosticLevel,
    DiagnosticSink,
)
from yai_core.discovery import (
    DiscoveredCandidate,
    SemanticCatalog,
    StaticCatalog,
    build_spec,
    discover,
)
from yai_core.kernel import (
    AdaptiveRouter,
    AgentLoop,
    Budget,
    BudgetExceeded,
    BudgetTracker,
    Context,
    RouteDecision,
)
from yai_core.learning import ContextualBanditSelector
from yai_core.llm.fallback import FallbackModelProvider
from yai_core.llm.openai_compat import OpenAICompatProvider
from yai_core.llm.scripted import ScriptedModel
from yai_core.metrics import MetricsRecorder
from yai_core.spi import (
    EmbeddingProvider,
    SandboxResult,
    StorageCipher,
    ToolDiscovery,
    ToolSandbox,
)
from yai_core.tools import (
    CREATE_CODE_TOOL,
    REQUEST_CAPABILITY,
    CodeToolManager,
    ToolExecutor,
    ToolRegistry,
    build_composer_tool,
    build_composite_spec,
    build_create_code_tool,
    build_request_capability_tool,
)
from yai_core.types import (
    AgentEvent,
    ChatMessage,
    CompositeStep,
    EventType,
    ModelResponse,
    RunResult,
    Strategy,
    TokenUsage,
    ToolCallRequest,
    ToolSpec,
)

__version__ = "0.9.0"

__all__ = [
    "AgentCore",
    "CoreConfig",
    "DiagnosticEvent",
    "DiagnosticKind",
    "DiagnosticLevel",
    "DiagnosticSink",
    "AdaptiveRouter",
    "RouteDecision",
    "AgentLoop",
    "Budget",
    "BudgetExceeded",
    "BudgetTracker",
    "Context",
    "ContextualBanditSelector",
    "MetricsRecorder",
    "ToolRegistry",
    "ToolExecutor",
    "CodeToolManager",
    "build_composer_tool",
    "build_composite_spec",
    "build_create_code_tool",
    "build_request_capability_tool",
    "CREATE_CODE_TOOL",
    "REQUEST_CAPABILITY",
    "CompositeStep",
    "OpenAICompatProvider",
    "FallbackModelProvider",
    "ScriptedModel",
    "build_spec",
    "discover",
    "StaticCatalog",
    "SemanticCatalog",
    "DiscoveredCandidate",
    "EmbeddingProvider",
    "ToolDiscovery",
    "ToolSandbox",
    "SandboxResult",
    "StorageCipher",
    "AgentEvent",
    "ChatMessage",
    "EventType",
    "ModelResponse",
    "RunResult",
    "Strategy",
    "TokenUsage",
    "ToolCallRequest",
    "ToolSpec",
    "__version__",
]
