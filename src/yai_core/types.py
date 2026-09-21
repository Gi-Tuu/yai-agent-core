"""YAI Agent Core 的核心数据类型。

刻意只依赖标准库：内核本体零第三方依赖，第三方能力全部通过 SPI 注入。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


class Strategy(StrEnum):
    """Adaptive Router 可选的执行策略。"""

    DIRECT = "direct"      # 无需工具，模型直接回答
    REACT = "react"        # 工具循环：推理 -> 调用 -> 观察 -> 再推理
    PLAN = "plan"          # 先拆解计划，再按计划执行工具循环
    CLARIFY = "clarify"    # 意图不清，先向宿主/用户反问


class EventType(StrEnum):
    """Observer 事件流类型——每一次"自适应决策"都必须可观测。"""

    STRATEGY_SELECTED = "strategy_selected"
    PLAN_CREATED = "plan_created"
    MODEL_MESSAGE = "model_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PERMISSION_ASKED = "permission_asked"
    CLARIFY_REQUESTED = "clarify_requested"
    CAPABILITY_MISSING = "capability_missing"
    TOOL_DISCOVERED = "tool_discovered"
    TOOL_COMPOSED = "tool_composed"
    CODE_TOOL_CREATED = "code_tool_created"
    CODE_TOOL_RETIRED = "code_tool_retired"
    ERROR = "error"
    DONE = "done"


@dataclass
class CompositeStep:
    """组合工具中的一步：调用一个已注册的内部工具，参数可引用前序结果。

    - tool：被组合的内部工具名（必须是宿主已注册的工具，组合不产生新原始能力）；
    - args：传给该工具的参数，支持占位符 ``{{$input.x}}``（组合工具入参）、
      ``{{$steps.0}}``（第 0 步完整结果）、``{{$steps.0.field}}``（字典字段）。
    """

    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    """统一工具规格。Native / OpenAPI / MCP / 组合工具在 Registry 中同构。"""

    name: str
    description: str
    input_schema: dict[str, Any]          # JSON Schema（与 MCP tools 形状一致）
    handler: Callable[..., Any] | None    # 同步函数；异步函数同样支持；组合/代码工具为 None
    source: Literal["native", "openapi", "mcp", "composite", "code"] = "native"
    # 仅 source == "composite" 时使用：按顺序编排的内部工具步骤。
    steps: list[CompositeStep] | None = None
    # 仅 source == "code" 时使用：模型生成、需在宿主沙箱里执行的 Python 代码。
    # 内核不内置任何代码执行器（见 ToolSandbox SPI），handler 保持 None。
    code: str | None = None

    def llm_schema(self) -> dict[str, Any]:
        """转换成 OpenAI 兼容的 function-calling 工具描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass
class ChatMessage:
    role: Role
    content: str = ""
    # assistant 发起的工具调用：[{"id","name","arguments"}]
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_llm_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelResponse:
    """ModelProvider 的统一返回，屏蔽各家 SDK 差异。"""

    content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    raw: Any = None


@dataclass
class AgentEvent:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    strategy: Strategy
    events: list[AgentEvent]
    final_text: str
