"""内核 meta-tool：由 Agent Loop 直接拦截处理、不交给普通执行器的工具。

这些工具同样注册到 ToolRegistry（这样模型才能在 function-calling 里看到并
调用它们），但它们驱动的是"内核自身的能力扩展"——运行中请求发现新工具、
创建代码工具——而不是某个宿主业务能力。因此 Agent Loop 在 react 循环里
按名字识别并处理：走统一权限闸（authorize_tool_call）、发专属事件，
不会进入 ToolExecutor 的普通 handler 执行路径。
"""

from __future__ import annotations

from yai_core.types import ToolSpec

#: 运行中请求发现新能力的 meta-tool 名。
REQUEST_CAPABILITY = "request_capability"
#: 运行中创建代码工具的 meta-tool 名（仅在宿主提供沙箱时注册）。
CREATE_CODE_TOOL = "create_code_tool"


def _unreachable(**_kwargs: object) -> dict:  # pragma: no cover - 由 loop 拦截
    raise RuntimeError("该 meta-tool 应由 Agent Loop 直接处理，不应进入执行器")


def build_request_capability_tool() -> ToolSpec:
    """构造 ``request_capability``：模型在执行中发现缺工具时显式声明能力缺口。"""
    schema = {
        "type": "object",
        "properties": {
            "need": {
                "type": "string",
                "description": (
                    "用一句话描述你缺失、但完成当前任务所必需的能力，"
                    "例如'查询指定城市的实时天气'。"
                ),
            },
        },
        "required": ["need"],
    }
    return ToolSpec(
        name=REQUEST_CAPABILITY,
        description=(
            "当你确认现有工具不足以完成任务时调用，用一句话声明缺失的能力；"
            "内核会尝试从宿主的能力目录/插件中发现并注册匹配的新工具，"
            "成功后下一轮即可调用。能靠现有工具完成时不要调用。"
        ),
        input_schema=schema,
        handler=_unreachable,
        source="native",
    )


def build_create_code_tool() -> ToolSpec:
    """构造 ``create_code_tool``：模型在需要新计算逻辑时生成代码工具。

    仅当宿主提供了 ToolSandbox 时内核才注册它。创建是敏感动作，默认走
    授权闸；代码在沙箱中执行，默认无网络、无敏感文件、有超时，并按 48h
    TTL 管理生命周期。优先用 compose_tool 组合现有工具，确实需要宿主
    尚未提供的新逻辑时才创建代码工具。
    """
    schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "新工具的蛇形命名，例如 weighted_score。",
            },
            "description": {
                "type": "string",
                "description": "一句话说明新工具的用途与输入输出，供后续路由使用。",
            },
            "input_schema": {
                "type": "object",
                "description": "新工具入参的 JSON Schema（object 类型）。",
            },
            "code": {
                "type": "string",
                "description": (
                    "实现该工具的 Python 代码：定义函数 run(inputs: dict)，"
                    "返回可 JSON 序列化的结果；不得访问网络或敏感文件。"
                ),
            },
        },
        "required": ["name", "description", "input_schema", "code"],
    }
    return ToolSpec(
        name=CREATE_CODE_TOOL,
        description=(
            "当且仅当现有工具和组合工具都无法完成任务时，创建一个由一段 "
            "Python 代码实现的新工具。代码在宿主隔离沙箱中运行，默认无网络、"
            "无敏感文件访问、有超时，默认 48 小时后回收。优先用 compose_tool "
            "组合现有工具；不要创建危险或越权的工具。"
        ),
        input_schema=schema,
        handler=_unreachable,
        source="native",
    )
