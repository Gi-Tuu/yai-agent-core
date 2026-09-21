"""组合工具（Composite Tool）：把宿主已注册的工具编排成一个新工具。

安全边界（设计红线）：
- 组合工具**只能引用宿主已注册的工具**，引用未注册工具直接拒绝创建；
- 组合工具执行时**每一步都走权限链路**（PermissionPolicy + Channel 确认），
  因此它的能力上限 = 被组合工具的并集，且不超过宿主已授权范围，物理上不越界；
- 不执行任何模型生成的代码，只做"已有工具的确定性编排"，所以零沙箱风险；
- 暂不支持嵌套组合（组合工具引用另一个组合工具），避免递归与权限放大。

参数占位符：
- ``{{$input.xxx}}``：组合工具被调用时收到的入参字段；
- ``{{$steps.0}}``：第 0 步的完整结果；
- ``{{$steps.0.field}}``：第 0 步结果字典里的某个字段（支持点路径）。
"""

from __future__ import annotations

import re
from typing import Any

from yai_core.tools.registry import ToolRegistry
from yai_core.types import CompositeStep, ToolSpec

# 匹配 {{$input.x}} / {{$steps.0.field}}（允许空白）。
_PLACEHOLDER = re.compile(r"\{\{\s*\$((?:input|steps)(?:\.[\w一-鿿-]+)*)\s*\}\}")
_FULL_PLACEHOLDER = re.compile(r"^\{\{\s*\$((?:input|steps)(?:\.[\w一-鿿-]+)*)\s*\}\}$")


def _lookup(path: str, context: dict[str, Any]) -> Any:
    """按点路径从 context 取值：input.x / steps.0 / steps.0.field。"""
    parts = path.split(".")
    cur: Any = context.get(parts[0])
    for key in parts[1:]:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and key.isdigit():
            idx = int(key)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
    return cur


def resolve_args(value: Any, context: dict[str, Any]) -> Any:
    """递归解析参数里的占位符；整串是单个占位符时保留原始类型（不强制转字符串）。"""
    if isinstance(value, str):
        full = _FULL_PLACEHOLDER.match(value.strip())
        if full:
            return _lookup(full.group(1), context)
        return _PLACEHOLDER.sub(
            lambda m: "" if _lookup(m.group(1), context) is None
            else str(_lookup(m.group(1), context)),
            value,
        )
    if isinstance(value, dict):
        return {k: resolve_args(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_args(v, context) for v in value]
    return value


def build_composite_spec(
    name: str,
    description: str,
    steps: list[dict[str, Any]] | list[CompositeStep],
    *,
    input_schema: dict[str, Any] | None = None,
) -> ToolSpec:
    """把声明式步骤构造成一个组合工具规格（不负责注册/校验）。"""
    parsed: list[CompositeStep] = []
    for step in steps:
        if isinstance(step, CompositeStep):
            parsed.append(step)
        else:
            tool = str(step.get("tool", "")).strip()
            if not tool:
                raise ValueError("组合工具的每个步骤都必须包含 tool 字段")
            parsed.append(CompositeStep(tool=tool, args=dict(step.get("args", {}))))
    return ToolSpec(
        name=name,
        description=description,
        input_schema=input_schema or {"type": "object", "properties": {}},
        handler=None,
        source="composite",
        steps=parsed,
    )


def build_composer_tool(registry: ToolRegistry) -> ToolSpec:
    """构造内置 meta-tool ``compose_tool``：让模型在运行时把现有工具编排成新工具。

    该工具本身是"写操作"（会改变注册表），执行时照常走权限链路；
    宿主需要显式注册它（AgentCore(composition=True)），默认不启用。
    """

    def compose_tool(
        name: str,
        description: str,
        steps: list[dict[str, Any]],
        input_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = (name or "").strip()
        description = (description or "").strip()
        if not name or not description:
            raise ValueError("组合工具必须提供非空的 name 和 description")
        if not isinstance(steps, list) or not steps:
            raise ValueError("组合工具必须包含至少一个步骤")
        if registry.has(name):
            raise ValueError(f"工具名 {name!r} 已存在，不能重复创建")
        for raw in steps:
            tool = str(raw.get("tool", "")).strip()
            if not registry.has(tool):
                # 安全红线：只允许编排宿主已声明的工具，杜绝凭空创造能力。
                raise ValueError(
                    f"步骤引用了未注册工具 {tool!r}，组合工具不能创造宿主没有的能力"
                )
            if registry.get(tool).source == "composite":
                raise ValueError("暂不支持嵌套组合工具（组合工具不能引用另一个组合工具）")
        spec = build_composite_spec(
            name, description, steps, input_schema=input_schema
        )
        registry.register(spec)
        return {
            "created": name,
            "description": description,
            "steps": [s.tool for s in spec.steps],  # type: ignore[union-attr]
        }

    schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "新组合工具的英文蛇形命名，如 weekly_review",
            },
            "description": {
                "type": "string",
                "description": "一句话说明这个组合工具做什么、何时使用",
            },
            "steps": {
                "type": "array",
                "description": "按顺序执行的步骤，每步引用一个已注册工具",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "description": "已注册的工具名"},
                        "args": {
                            "type": "object",
                            "description": "工具参数，可用 {{$input.x}} / {{$steps.0.field}} 引用",
                        },
                    },
                    "required": ["tool"],
                },
            },
            "input_schema": {
                "type": "object",
                "description": "可选，组合工具自身入参的 JSON Schema",
            },
        },
        "required": ["name", "description", "steps"],
    }
    return ToolSpec(
        name="compose_tool",
        description=(
            "当现有工具需要多步固定编排、且每一步都是宿主已提供的工具时，"
            "把它们组合成一个可复用的新工具。只能引用已注册工具，不能创造新能力；"
            "组合工具执行时每一步仍会按权限策略确认。"
        ),
        input_schema=schema,
        handler=compose_tool,
        source="native",
    )
