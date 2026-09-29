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

    #: 合法的工具来源（manifest 反序列化时校验，防止脏数据进入注册表）。
    _SOURCES = ("native", "openapi", "mcp", "composite", "code")

    def to_manifest_dict(self) -> dict[str, Any]:
        """导出语言中立、可 JSON 序列化的能力清单（**不含运行时 handler**）。

        - composite：随清单携带 ``steps``（每步 {tool, args}），工作流即数据；
        - code：随清单携带 ``code`` 字符串（执行仍需宿主沙箱）；
        - native/openapi/mcp：handler 是运行时对象、不可移植，只导出声明。
        """
        manifest: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "source": self.source,
        }
        if self.source == "composite" and self.steps:
            manifest["steps"] = [
                {"tool": step.tool, "args": step.args} for step in self.steps
            ]
        if self.source == "code":
            manifest["code"] = self.code
        return manifest

    @classmethod
    def from_manifest_dict(cls, data: dict[str, Any]) -> ToolSpec:
        """从语言中立清单重建工具规格；``handler`` 始终为 ``None``。

        composite / code 的可执行信息（steps / code）随清单携带，注册后即可
        （在宿主沙箱内）执行；native/openapi/mcp 仅为能力声明，需另行绑定
        执行后端（跨语言接入请直接走 MCP / OpenAPI 集成，它们自带 handler）。
        """
        if not isinstance(data, dict):
            raise TypeError(f"manifest 工具必须是 object，得到 {type(data)!r}")
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("manifest 工具缺少非空 name")
        source = data.get("source", "native")
        if source not in cls._SOURCES:
            raise ValueError(
                f"manifest 工具 {name!r} 的 source 非法: {source!r}，"
                f"合法值 {cls._SOURCES}"
            )

        steps: list[CompositeStep] | None = None
        if source == "composite":
            raw_steps = data.get("steps")
            if not isinstance(raw_steps, list) or not raw_steps:
                raise ValueError(f"组合工具 {name!r} 的 manifest 缺少非空 steps")
            steps = []
            for index, raw in enumerate(raw_steps):
                if not isinstance(raw, dict):
                    raise ValueError(
                        f"组合工具 {name!r} 第 {index + 1} 步必须是 object"
                    )
                tool = str(raw.get("tool", "")).strip()
                if not tool:
                    raise ValueError(
                        f"组合工具 {name!r} 第 {index + 1} 步缺少 tool"
                    )
                steps.append(
                    CompositeStep(tool=tool, args=dict(raw.get("args", {})))
                )

        code = data.get("code") if source == "code" else None
        return cls(
            name=name,
            description=str(data.get("description", "")),
            input_schema=dict(data.get("input_schema") or {}),
            handler=None,
            source=source,  # type: ignore[arg-type]
            steps=steps,
            code=code,
        )


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
