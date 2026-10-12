"""OpenTelemetry 结构化 tracing 集成（可选 ``otel`` extra，懒加载）。

用法见 :mod:`yai_core.integrations.tracing.otel` 与 ``docs/tracing.md``：

    from yai_core.integrations.tracing import traced_astream, traced_run

    async for event in traced_astream(core, "整理本周笔记"):
        ...  # 与 core.astream 事件完全一致，外侧额外产出一条 trace

内核本体不依赖 opentelemetry；未安装 ``otel`` extra 时模块仍可导入，只有真正开始
记录 trace 才会抛出带安装指引的 ImportError。
"""

from yai_core.integrations.tracing.otel import (
    ROOT_SPAN_NAME,
    TOOL_SPAN_PREFIX,
    traced_astream,
    traced_run,
)

__all__ = [
    "traced_astream",
    "traced_run",
    "ROOT_SPAN_NAME",
    "TOOL_SPAN_PREFIX",
]
