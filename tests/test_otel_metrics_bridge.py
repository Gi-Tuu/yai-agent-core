"""OTel Metrics 桥接测试：observable 仪器在采集时从 core 拉取快照（离线）。"""

from __future__ import annotations

import asyncio

import pytest


def _collect_points(metrics_data) -> dict[str, list[tuple]]:
    """把 SDK metrics_data 摊平为 {指标名: [(value, {属性}), ...]}。"""
    points: dict[str, list[tuple]] = {}
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                data_points = getattr(metric.data, "data_points", [])
                for dp in data_points:
                    points.setdefault(metric.name, []).append(
                        (dp.value, dict(getattr(dp, "attributes", {}) or {}))
                    )
    return points


def test_otel_meter_bridges_core_snapshot() -> None:
    sdk = pytest.importorskip("opentelemetry.sdk.metrics")
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from yai_core import AgentCore, ModelResponse, build_spec
    from yai_core.integrations.metrics import register_otel_meter

    class ScriptedModel:
        async def achat(self, messages, tools=None, *, tier="standard"):
            return ModelResponse(content="你好")

    def list_notes() -> list:
        """列出笔记。"""
        return [{"id": 1}]

    reader = InMemoryMetricReader()
    provider = sdk.MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("yai-test")

    core = AgentCore(ScriptedModel())
    core.register_tools([build_spec(list_notes)])
    register_otel_meter(core, meter=meter)

    asyncio.run(core.run("你好"))

    points = _collect_points(reader.get_metrics_data())

    # runs{outcome=completed} 至少 1。
    runs = {attrs.get("outcome"): value for value, attrs in points.get("yai.agent.runs", [])}
    assert runs.get("completed") >= 1
    assert runs.get("started") >= 1

    # 模型调用按档位。
    tiers = {attrs.get("tier"): value for value, attrs in points.get("yai.agent.model.calls", [])}
    assert tiers.get("standard") >= 1

    # 时延累计桶：+Inf 即样本计数。
    duration = {
        attrs.get("le"): value for value, attrs in points.get("yai.agent.run.duration", [])
    }
    assert duration.get("+Inf") >= 1
    assert any(name == "yai.agent.run.duration.sum" for name in points)

    # uptime gauge 存在且非负。
    assert points.get("yai.agent.uptime")
    assert points["yai.agent.uptime"][0][0] >= 0

    # 生命周期事件计数被桥接（direct run 必发 strategy_selected / done）。
    event_types = {attrs.get("type") for _, attrs in points.get("yai.agent.events", [])}
    assert "done" in event_types
