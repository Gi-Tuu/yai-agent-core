"""把零依赖 :class:`~yai_core.metrics.MetricsRecorder` 桥接到 OpenTelemetry Metrics。

设计红线与 :mod:`yai_core.integrations.tracing.otel` 完全对齐：

- **可选依赖、懒加载**：``opentelemetry`` 只在 ``otel`` extra 里，模块顶层不 import；
  内核本体 ``dependencies`` 仍为空，未安装时给出明确安装指引。
- **只做 instrumentation library，不初始化 MeterProvider / Reader / Exporter**：配置
  OTLP / Prometheus / 内存 reader 是宿主进程的职责（OTel 惯例）。宿主配好后端后把
  ``meter`` 传进来，或直接用全局 ``metrics.get_meter(...)``（未配置时是 no-op）。
- **采集时 pull，不订阅、不侵入**：全部用 *observable* 仪器，回调在后端每次采集
  （scrape / collect）时才从 ``core.metrics_snapshot()`` 拉一次最新值。run 热路径上
  不会因为挂了 OTel 而多任何一次回调。

指标命名用 OTel 惯用的点号（``yai.agent.*``），经 OTel 的 Prometheus exporter 导出时
会自动变成下划线 + ``_total`` 等惯例名。run 时延没有 observable histogram（OTel 仅支持
在事件发生时 record 的 histogram），因此用一组累计桶 observable counter（``le`` 标签）
加 ``.sum`` 表达，信息与内核 ``/metrics`` 的直方图等价。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

_RUN_OUTCOMES = ("started", "completed", "failed", "cancelled")
_MODEL_TIERS = ("standard", "strong")


def _load_otel(meter: Any | None) -> tuple[Any, Any, Any]:
    """懒加载 OpenTelemetry Metrics API，返回 ``(metrics 模块, Observation, meter)``。"""
    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry.metrics import Observation
    except ImportError as exc:  # pragma: no cover - 取决于宿主是否安装 extra
        raise ImportError(
            "指标桥接需要可选依赖 otel，请先安装：pip install "
            '"yai-agent-core[otel]"（内核本体保持零硬依赖）。'
        ) from exc
    if meter is None:
        meter = otel_metrics.get_meter("yai-agent-core")
    return otel_metrics, Observation, meter


def register_otel_meter(core: Any, *, meter: Any | None = None) -> Any:
    """在给定 ``meter`` 上注册一组 observable 仪器，采集时从 core 拉取指标快照。

    返回该 meter（便于宿主在测试中继续断言）。注册一次即可长期被后端周期采集。
    """
    _metrics_mod, Observation, otel_meter = _load_otel(meter)

    def observe(
        value: float | int, attributes: dict[str, str] | None = None
    ) -> Any:
        return Observation(value, attributes=attributes)

    def runs_callback(_options: Any) -> Iterable[Any]:
        runs = core.metrics_snapshot()["runs"]
        return [observe(runs[outcome], {"outcome": outcome}) for outcome in _RUN_OUTCOMES]

    def strategy_callback(_options: Any) -> Iterable[Any]:
        return [
            observe(count, {"strategy": strategy})
            for strategy, count in core.metrics_snapshot()["runs_by_strategy"].items()
        ]

    def tools_callback(_options: Any) -> Iterable[Any]:
        observations: list[Any] = []
        for name, counts in core.metrics_snapshot()["tool_calls"].items():
            ok = max(counts["total"] - counts["errors"], 0)
            observations.append(observe(ok, {"tool": name, "result": "ok"}))
            observations.append(observe(counts["errors"], {"tool": name, "result": "error"}))
        return observations

    def model_callback(_options: Any) -> Iterable[Any]:
        model = core.metrics_snapshot()["model_calls"]
        return [observe(model[tier], {"tier": tier}) for tier in _MODEL_TIERS]

    def events_callback(_options: Any) -> Iterable[Any]:
        return [
            observe(count, {"type": etype})
            for etype, count in core.metrics_snapshot()["events"].items()
        ]

    def duration_buckets_callback(_options: Any) -> Iterable[Any]:
        buckets = core.metrics_snapshot()["run_duration_seconds"]["buckets"]
        return [observe(count, {"le": label}) for label, count in buckets.items()]

    def duration_sum_callback(_options: Any) -> Iterable[Any]:
        return [observe(core.metrics_snapshot()["run_duration_seconds"]["sum"])]

    def uptime_callback(_options: Any) -> Iterable[Any]:
        return [observe(core.metrics_snapshot()["uptime_seconds"])]

    otel_meter.create_observable_counter(
        "yai.agent.runs",
        callbacks=[runs_callback],
        description="Agent runs by outcome.",
    )
    otel_meter.create_observable_counter(
        "yai.agent.runs.by_strategy",
        callbacks=[strategy_callback],
        description="Runs by selected routing strategy.",
    )
    otel_meter.create_observable_counter(
        "yai.agent.tool.calls",
        callbacks=[tools_callback],
        description="Tool invocations by tool name and result.",
    )
    otel_meter.create_observable_counter(
        "yai.agent.model.calls",
        callbacks=[model_callback],
        description="Model calls by cost tier.",
    )
    otel_meter.create_observable_counter(
        "yai.agent.events",
        callbacks=[events_callback],
        description="Adaptive lifecycle events by type.",
    )
    # 累计桶（le 标签，含 +Inf 即 count）与 sum，等价于 run 时延直方图。
    otel_meter.create_observable_counter(
        "yai.agent.run.duration",
        callbacks=[duration_buckets_callback],
        description="Cumulative run-duration histogram buckets (le labels, +Inf=count).",
        unit="s",
    )
    otel_meter.create_observable_counter(
        "yai.agent.run.duration.sum",
        callbacks=[duration_sum_callback],
        description="Cumulative sum of run durations.",
        unit="s",
    )
    otel_meter.create_observable_gauge(
        "yai.agent.uptime",
        callbacks=[uptime_callback],
        description="Process uptime since the metrics recorder was created.",
        unit="s",
    )
    return otel_meter
