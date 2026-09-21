"""组合工具（Composite Tool）测试：能力边界内编排、每步走权限、LLM 在线创建。

全部离线，使用 ScriptedModel，不需要 API Key。
"""

import asyncio
import json

import pytest

from yai_core import (
    AgentCore,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    build_composer_tool,
    build_composite_spec,
    build_spec,
)
from yai_core.spi import PermissionDecision


def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


def mul(a: int, b: int) -> int:
    """两数相乘。"""
    return a * b


def _core_with_arith(model, **kwargs):
    core = AgentCore(model, **kwargs)
    core.register_tools([build_spec(add), build_spec(mul)])
    return core


def test_composite_executes_steps_with_input_and_prior_result_refs() -> None:
    """组合工具按序执行：(x+1) 再翻倍，验证 $input / $steps 占位符解析。"""
    model = ScriptedModel([ModelResponse(content="完成")])
    core = _core_with_arith(model, auto_approve_tools=True)
    core.register_tools(
        [
            build_composite_spec(
                "add_then_double",
                "先加 1 再翻倍",
                [
                    {"tool": "add", "args": {"a": "{{$input.x}}", "b": 1}},
                    {"tool": "mul", "args": {"a": "{{$steps.0}}", "b": 2}},
                ],
            )
        ]
    )

    events, ok, text = asyncio.run(
        core.executor.execute("add_then_double", {"x": 5})
    )

    assert ok
    assert json.loads(text) == {"steps": [6, 12]}
    types = [e.type.value for e in events]
    assert types[0] == "tool_composed"  # 组合工具开始即发编排事件
    composed = events[0].data
    assert composed["tool"] == "add_then_double"
    assert composed["steps"] == ["add", "mul"]


def test_compose_tool_rejects_unregistered_inner_tool() -> None:
    """安全红线：组合工具不能引用宿主未注册的工具（不创造新原始能力）。"""
    model = ScriptedModel([ModelResponse(content="unused")])
    core = _core_with_arith(model, auto_approve_tools=True)
    core.register_tools([build_composer_tool(core.registry)])

    composer = core.registry.get("compose_tool")
    with pytest.raises(ValueError, match="未注册工具"):
        composer.handler(
            name="evil",
            description="越界组合",
            steps=[{"tool": "add"}, {"tool": "shell_exec"}],
        )
    # 拒绝后不得注册
    assert not core.registry.has("evil")


def test_compose_tool_rejects_nested_composite() -> None:
    """暂不支持嵌套组合：组合工具引用另一个组合工具应被拒绝。"""
    model = ScriptedModel([ModelResponse(content="unused")])
    core = _core_with_arith(model, auto_approve_tools=True)
    core.register_tools(
        [
            build_composite_spec(
                "first_combo", "一", [{"tool": "add", "args": {"a": 1, "b": 1}}]
            ),
            build_composer_tool(core.registry),
        ]
    )
    composer = core.registry.get("compose_tool")
    with pytest.raises(ValueError, match="嵌套组合"):
        composer.handler(
            name="second_combo",
            description="嵌套",
            steps=[{"tool": "first_combo"}],
        )


def test_composite_each_step_goes_through_permission_and_deny_short_circuits() -> None:
    """组合工具每一步都走权限链路：策略拒绝第 1 步时整体失败，不越权执行后续。"""

    class DenyPolicy:
        async def check(self, name, arguments):
            return PermissionDecision.DENY

    model = ScriptedModel([ModelResponse(content="unused")])
    core = _core_with_arith(model, policy=DenyPolicy())
    core.register_tools(
        [
            build_composite_spec(
                "add_then_double",
                "先加再乘",
                [
                    {"tool": "add", "args": {"a": "{{$input.x}}", "b": 1}},
                    {"tool": "mul", "args": {"a": "{{$steps.0}}", "b": 2}},
                ],
            )
        ]
    )

    events, ok, text = asyncio.run(
        core.executor.execute("add_then_double", {"x": 5})
    )
    assert not ok
    assert "第 1 步" in text
    # 被拒后不应执行到 mul
    called = [e.data.get("tool") for e in events if e.type.value == "tool_call"]
    assert "mul" not in called


def test_llm_composes_then_uses_new_tool_end_to_end() -> None:
    """模型先调 compose_tool 创造组合工具，下一轮即可调用，全程发 tool_composed 事件。"""
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="compose_tool",
                        arguments={
                            "name": "add_then_double",
                            "description": "先加 1 再翻倍",
                            "steps": [
                                {"tool": "add", "args": {"a": "{{$input.x}}", "b": 1}},
                                {"tool": "mul", "args": {"a": "{{$steps.0}}", "b": 2}},
                            ],
                        },
                    )
                ],
            ),
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c2", name="add_then_double", arguments={"x": 5}
                    )
                ],
            ),
            ModelResponse(content="结果是 12。"),
        ]
    )
    core = _core_with_arith(
        model, composition=True, auto_approve_tools=True
    )

    result = asyncio.run(core.run("计算 x=5 加一翻倍"))

    types = [e.type.value for e in result.events]
    assert "tool_composed" in types
    # compose_tool 成功创建
    create_result = next(
        e.data for e in result.events
        if e.type.value == "tool_result" and e.data.get("tool") == "compose_tool"
    )
    assert create_result["ok"]
    # 新组合工具被实际执行且成功
    combo_result = next(
        e.data for e in result.events
        if e.type.value == "tool_result" and e.data.get("tool") == "add_then_double"
    )
    assert combo_result["ok"]
    assert "12" in result.final_text
