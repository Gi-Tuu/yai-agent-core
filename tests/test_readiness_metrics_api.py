"""/readyz 与 /metrics 端点测试：离线 TestClient，不依赖网络或真实模型。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from yai_core import AgentCore, ModelResponse, build_spec
from yai_core.batteries.fastapi_server import create_app
from yai_core.batteries.fastapi_server.readiness import _interpret, _run_one, run_probes


class ScriptedModel:
    def __init__(self, text: str) -> None:
        self._text = text

    async def achat(self, messages, tools=None, *, tier="standard"):
        return ModelResponse(content=self._text)


def list_notes() -> list:
    """列出全部笔记。"""
    return [{"id": 1, "title": "Core 骨架"}]


def _core() -> AgentCore:
    core = AgentCore(ScriptedModel("你好，我是内嵌助手。"))
    core.register_tools([build_spec(list_notes)])
    return core


def _client(monkeypatch, **kwargs) -> TestClient:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    monkeypatch.setenv("YAI_PROJECT_SLUG", "yai-test")
    return TestClient(create_app(_core(), **kwargs))


def test_readyz_reports_default_probes(monkeypatch) -> None:
    resp = _client(monkeypatch).get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["tools"]["status"] == "ok"
    assert body["checks"]["tools"]["registered_tools"] >= 1
    assert body["checks"]["memory"]["status"] == "ok"
    assert "backend" in body["checks"]["memory"]


def test_readyz_503_when_a_probe_fails(monkeypatch) -> None:
    def down_probe() -> str:
        return "database unreachable"

    resp = _client(monkeypatch, readiness_probes={"database": down_probe}).get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["status"] == "error"
    assert "database unreachable" in body["checks"]["database"]["detail"]
    # 默认探针仍正常，失败被隔离在单个探针。
    assert body["checks"]["tools"]["status"] == "ok"


def test_readyz_503_on_slow_probe_timeout(monkeypatch) -> None:
    async def slow_probe() -> None:
        await asyncio.sleep(0.3)

    client = _client(monkeypatch, readiness_probes={"slow": slow_probe}, readiness_timeout=0.05)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["slow"]["status"] == "error"
    assert "timed out" in resp.json()["checks"]["slow"]["detail"]


def test_metrics_default_prometheus_and_json(monkeypatch) -> None:
    client = _client(monkeypatch)
    # 先跑一次 run，让指标非空。
    assert client.post("/v1/agent/run", json={"task": "你好"}).status_code == 200

    text_resp = client.get("/metrics")
    assert text_resp.status_code == 200
    assert text_resp.headers["content-type"].startswith("text/plain")
    assert "yai_agent_runs_total" in text_resp.text
    assert "yai_run_duration_seconds_bucket" in text_resp.text

    json_resp = client.get("/metrics?format=json")
    assert json_resp.status_code == 200
    snap = json_resp.json()
    assert snap["runs"]["completed"] >= 1
    assert "run_duration_seconds" in snap


def test_get_probe_paths_are_not_rate_limited(monkeypatch) -> None:
    client = _client(monkeypatch)
    # 高频抓取探针 / 指标，任何一次都不应被 429（限流只拦 POST /v1/agent/run）。
    for _ in range(60):
        assert client.get("/readyz").status_code == 200
        assert client.get("/health").status_code == 200
        assert client.get("/metrics").status_code == 200


def test_metrics_protected_by_api_key_but_probes_stay_public(monkeypatch) -> None:
    monkeypatch.setenv("YAI_API_KEY", "secret-key")
    client = _client(monkeypatch)
    # 配了 Key：/metrics 默认要鉴权。
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer secret-key"}).status_code == 200
    # liveness / readiness 恒公开，编排探针不带 Key 也必须能抓。
    assert client.get("/health").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_metrics_can_be_explicitly_public(monkeypatch) -> None:
    monkeypatch.setenv("YAI_API_KEY", "secret-key")
    monkeypatch.setenv("YAI_METRICS_PUBLIC", "1")
    client = _client(monkeypatch)
    assert client.get("/metrics").status_code == 200  # Prometheus 匿名可抓
    assert client.get("/v1/tools").status_code == 401  # 业务路径仍受保护


# ---------- readiness 纯函数边界：返回值归一化 / 异常隔离 / 空探针 ----------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (None, {"status": "ok"}),
        (True, {"status": "ok"}),
        (False, {"status": "error", "detail": "probe returned False"}),
        ("db down", {"status": "error", "detail": "db down"}),
        ({"ok": True, "n": 2}, {"status": "ok", "n": 2}),
        ({"ok": False, "reason": "r"}, {"status": "error", "reason": "r"}),
        (123, {"status": "error", "detail": "unsupported probe result: int"}),
    ],
)
def test_interpret_normalizes_probe_results(result, expected) -> None:
    assert _interpret(result) == expected


def test_run_one_isolates_probe_exception() -> None:
    def boom() -> None:
        raise RuntimeError("kaboom")

    name, outcome = asyncio.run(_run_one("broken", boom, timeout=1.0))
    assert name == "broken"
    # 探针抛异常只影响自身，被转成结构化失败而不是冒泡拖垮整个 /readyz。
    assert outcome["status"] == "error"
    assert "RuntimeError" in outcome["detail"]
    assert "kaboom" in outcome["detail"]


def test_run_one_accepts_async_probe() -> None:
    async def healthy() -> dict:
        return {"ok": True, "ready": True}

    _, outcome = asyncio.run(_run_one("async_ok", healthy, timeout=1.0))
    assert outcome == {"status": "ok", "ready": True}


def test_run_probes_empty_is_ready() -> None:
    # 宿主一个探针都没注册时，readiness 应判为就绪而非报错。
    ready, checks = asyncio.run(run_probes({}))
    assert ready is True
    assert checks == {}


def test_run_probes_aggregates_mixed_results() -> None:
    def good() -> bool:
        return True

    def bad() -> bool:
        return False

    ready, checks = asyncio.run(run_probes({"good": good, "bad": bad}))
    assert ready is False
    assert checks["good"]["status"] == "ok"
    assert checks["bad"]["status"] == "error"


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
