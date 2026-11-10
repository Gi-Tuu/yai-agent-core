"""跨核调用可视化：把专员事件流里的跨核工具调用聚合成 edge 视图。

父核通过联邦调用子核时，工具名形如 ``notes__search_notes``（``前缀__工具名``）。
本模块不依赖 Qt：工作台在事件转发时喂给它，QML 只读工作台聚合后的
``crossCoreEdges``；这样面板逻辑可以离线测试，也能被未来的审计视图复用。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class CrossCoreEdge:
    """一条"父核 → 子核"的调用边累计。"""

    prefix: str
    calls: int = 0
    failures: int = 0
    tokens: int = 0
    last_tool: str = ""
    last_ok: bool | None = None
    usage: dict[str, float] = field(default_factory=dict)

    def as_row(self) -> dict[str, object]:
        """QML 友好的一行（短字段名，避免把整个使用量明细塞进面板）。"""
        return {
            "prefix": self.prefix,
            "calls": self.calls,
            "failures": self.failures,
            "tokens": self.tokens,
            "last_tool": self.last_tool,
            "last_ok": self.last_ok,
        }


def split_cross_core_tool(tool: str | None) -> tuple[str, str] | None:
    """``notes__search_notes`` → ``("notes", "search_notes")``；不是跨核工具返回 None。

    只认**第一个** ``__`` 之前是非空前缀的形态；前缀里允许下划线（``notes_app__x``）。
    """
    if not tool or "__" not in tool:
        return None
    prefix, _, local = tool.partition("__")
    if not prefix or not local:
        return None
    return prefix, local


def _event_data(payload: object) -> dict[str, object]:
    if isinstance(payload, dict):
        return dict(payload)
    if not isinstance(payload, str) or not payload.strip():
        return {}
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _usage_tokens(usage: object) -> tuple[int, dict[str, float]]:
    """从 usage 里取 token 总数与数值明细；非数值字段忽略。"""
    if not isinstance(usage, dict):
        return 0, {}
    details: dict[str, float] = {}
    for key, value in usage.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        details[str(key)] = float(value)
    total = usage.get("total_tokens", usage.get("tokens"))
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        return int(total), details
    return int(sum(details.values())), details


class CrossCoreView:
    """跨核 edge 累计器：喂 ``tool_call`` / ``tool_result`` 事件，产出面板行。"""

    def __init__(self) -> None:
        self._edges: dict[str, CrossCoreEdge] = {}

    def clear(self) -> None:
        self._edges.clear()

    def observe(self, name: str, payload: object) -> list[CrossCoreEdge]:
        """处理一条事件；返回**本次被更新**的 edge（无跨核调用时为空）。

        - ``tool_call``：调用数 +1，记录最近工具，等待结果；
        - ``tool_result``：失败数 +1（``ok is False``），聚合 usage；
        - 其他事件（strategy_selected / done / ...）忽略。
        """
        if name not in {"tool_call", "tool_result"}:
            return []
        data = _event_data(payload)
        raw_tool = data.get("tool")
        split = split_cross_core_tool(raw_tool if isinstance(raw_tool, str) else None)
        if split is None:
            return []
        prefix, local = split
        edge = self._edges.get(prefix)
        if edge is None:
            # 结果先于调用到达（例如中途接入的事件流）不凭空造 edge，避免面板幽灵行。
            if name != "tool_call":
                return []
            edge = CrossCoreEdge(prefix=prefix)
            self._edges[prefix] = edge
        edge.last_tool = local
        if name == "tool_call":
            edge.calls += 1
            edge.last_ok = None
        else:
            ok = data.get("ok", True)
            edge.last_ok = bool(ok)
            if not edge.last_ok:
                edge.failures += 1
            tokens, details = _usage_tokens(data.get("usage"))
            edge.tokens += tokens
            for key, value in details.items():
                edge.usage[key] = float(edge.usage.get(key, 0.0)) + float(value)
        return [edge]

    def edges(self) -> list[dict[str, object]]:
        """按调用数降序的面板行（同数按前缀排序，保证渲染稳定）。"""
        ordered = sorted(self._edges.values(), key=lambda e: (-e.calls, e.prefix))
        return [edge.as_row() for edge in ordered]

    def summary(self) -> dict[str, float | int]:
        """一行汇总：edge 数 / 调用 / 失败 / tokens。"""
        return {
            "edges": len(self._edges),
            "calls": sum(edge.calls for edge in self._edges.values()),
            "failures": sum(edge.failures for edge in self._edges.values()),
            "tokens": sum(edge.tokens for edge in self._edges.values()),
        }

    def as_dict(self) -> dict[str, object]:
        """调试 / 测试用：完整明细（含 usage），不直接进 QML。"""
        return {
            "edges": [asdict(edge) for edge in self._edges.values()],
            "summary": self.summary(),
        }
