"""可选的指标后端桥接（extras）。内核本体零硬依赖，这里只放懒加载集成。"""

from yai_core.integrations.metrics.otel_meter import register_otel_meter

__all__ = ["register_otel_meter"]
