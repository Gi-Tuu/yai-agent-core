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
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class CompositeVersion:
    """组合工具的一个版本快照（纯数据）。

    快照里**只有步骤**（工具名 + 参数），没有 handler——组合工具本来就是"已有工具的
    编排"，它的可执行性来自被引用的工具，而不是这份数据。
    """

    name: str
    version: str
    description: str = ""
    steps: tuple[CompositeStep, ...] = ()

    def to_manifest_steps(self) -> list[dict[str, Any]]:
        return [{"tool": step.tool, "args": dict(step.args)} for step in self.steps]


@dataclass
class CompositeWorkflowStore:
    """组合工具的**版本仓库**：可命名、可回滚、可比对。

    为什么需要它：组合工具就是"一段工作流"，而工作流会改。没有版本的时候，
    "上周那套流程到底长什么样"没人答得上来；改了之后想退回，也只能凭记忆手改回去。

    口径：

    - 存的是**步骤快照**（数据），不存 handler、不存运行时状态；
    - ``save`` 同一 (name, version) 会**覆盖**（版本号是作者自己取的，重取即重定义）；
    - ``diff`` 给出结构化差异（增/删/改的步骤），供人看，不自动合并；
    - ``restore`` 造出可注册的 :class:`ToolSpec`（handler 仍为 None，组合工具的
      handler 由执行器按 composite 分支跑，不需要绑定）。
    """

    _versions: dict[str, dict[str, CompositeVersion]] = field(default_factory=dict)

    def save(self, spec: ToolSpec, *, version: str) -> CompositeVersion:
        """保存一个组合工具的版本快照；返回存下来的记录。"""
        label = (version or "").strip()
        if not label:
            raise ValueError("版本号不能为空")
        if spec.source != "composite":
            raise ValueError(f"只有组合工具可以存版本，收到 source={spec.source!r}")
        record = CompositeVersion(
            name=spec.name,
            version=label,
            description=spec.description,
            steps=tuple(spec.steps or ()),
        )
        self._versions.setdefault(spec.name, {})[label] = record
        return record

    def versions(self, name: str) -> list[str]:
        """列出某个组合工具的版本号（按创建顺序）。"""
        return list(self._versions.get(name, {}))

    def get(self, name: str, version: str) -> CompositeVersion:
        """取某个版本；不存在抛 KeyError。"""
        try:
            return self._versions[name][version]
        except KeyError as exc:
            raise KeyError(
                f"组合工具 {name!r} 没有版本 {version!r}（现有：{self.versions(name)}）"
            ) from exc

    def latest(self, name: str) -> str | None:
        """最近保存的版本号（没有则 None）。"""
        versions = self.versions(name)
        return versions[-1] if versions else None

    def diff(self, name: str, old: str, new: str) -> dict[str, Any]:
        """比对两个版本：给出步骤层面的增 / 删 / 改。

        改动的判定是"同一位置的步骤内容不同"（工具名或参数不同）——**按位置**比，
        因为工作流的语义就是顺序敏感：把第 2 步和第 3 步换位，对结果的影响可能
        远大于"改了一个参数"。
        """
        before = self.get(name, old)
        after = self.get(name, new)
        before_steps = before.to_manifest_steps()
        after_steps = after.to_manifest_steps()
        added: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        changed: list[dict[str, Any]] = []
        for index in range(max(len(before_steps), len(after_steps))):
            old_step = before_steps[index] if index < len(before_steps) else None
            new_step = after_steps[index] if index < len(after_steps) else None
            if old_step == new_step:
                continue
            if old_step is None:
                added.append(new_step or {})
            elif new_step is None:
                removed.append(old_step)
            else:
                changed.append({"index": index, "old": old_step, "new": new_step})
        return {
            "name": name,
            "old": old,
            "new": new,
            "added": added,
            "removed": removed,
            "changed": changed,
            "identical": not (added or removed or changed),
        }

    def restore(self, name: str, version: str) -> ToolSpec:
        """把某个版本还原成可注册的 :class:`ToolSpec`（供宿主回滚用）。"""
        record = self.get(name, version)
        return ToolSpec(
            name=record.name,
            description=record.description,
            input_schema={"type": "object", "properties": {}},
            handler=None,
            source="composite",
            steps=list(record.steps),
            version=record.version,
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
            "当新能力需要固定多步调用宿主【已提供】的工具时使用，包括先取数、再对"
            "结果加工汇总——组合工具能真正执行宿主工具，凡是复用 / 编排宿主能力的"
            "（哪怕之后还要计算）都用它。只能引用已注册工具、不能创造新能力，"
            "执行时每一步仍会按权限策略确认。若数据已在入参中、只需全新的纯计算"
            "算法（不回调任何宿主工具），则改用 create_code_tool。"
        ),
        input_schema=schema,
        handler=compose_tool,
        source="native",
    )
