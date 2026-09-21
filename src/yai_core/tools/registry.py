from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from yai_core.types import ToolSpec

# 轻量目录里每条摘要的最大长度：工具数量增长后，目录只保留一句话能力说明。
_CATALOG_SUMMARY_CHARS = 80


def _one_line(description: str, limit: int = _CATALOG_SUMMARY_CHARS) -> str:
    """把工具描述压成一句话摘要：取首行、去空白、超长截断。"""
    first = description.strip().splitlines()[0] if description.strip() else ""
    return first if len(first) <= limit else first[: limit - 1] + "…"


class ToolRegistry:
    """统一工具注册表：Native / OpenAPI / MCP 工具在此同构。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具 {spec.name!r} 已注册，名称必须唯一")
        self._tools[spec.name] = spec

    def register_many(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"未知工具 {name!r}，当前可用：{list(self._tools)}")
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def llm_schemas(self) -> list[dict]:
        return [spec.llm_schema() for spec in self._tools.values()]

    def catalog(self) -> list[dict[str, str]]:
        """轻量工具目录：只含 name + 一句话摘要。

        工具数量增长后，系统提示/能力检索先看目录，模型选定工具后再用
        ``schemas_for`` 取完整 JSON Schema，避免把上百个工具的参数结构一次性
        塞进上下文（两层工具目录）。
        """
        return [
            {"name": s.name, "summary": _one_line(s.description)}
            for s in self._tools.values()
        ]

    def schemas_for(self, names: Iterable[str]) -> list[dict[str, Any]]:
        """按名称返回完整 function-calling schema；未知名称抛 KeyError。"""
        return [self.get(name).llm_schema() for name in names]

    def describe(self) -> str:
        """给系统提示词用的工具清单文本。"""
        if not self._tools:
            return "（当前宿主没有提供任何工具）"
        return "\n".join(f"- {s.name}: {s.description}" for s in self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)
