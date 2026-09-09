"""FastAPI Battery 端点测试：离线脚本模型 + TestClient，不需要 API Key 与网络。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from yai_core import AgentCore, ModelResponse, build_spec
from yai_core.batteries.fastapi_server import create_app


class ScriptedModel:
    def __init__(self, text: str) -> None:
        self._text = text

    async def achat(self, messages, tools=None, *, tier="standard"):
        return ModelResponse(content=self._text)


def list_notes() -> list:
    """列出全部笔记。"""
    return [{"id": 1, "title": "Core 骨架"}]


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    monkeypatch.setenv("YAI_PROJECT_SLUG", "yai-test")
    core = AgentCore(ScriptedModel("你好，我是内嵌助手。"))
    core.register_tools([build_spec(list_notes)])
    return TestClient(create_app(core))


def test_health(monkeypatch) -> None:
    resp = _client(monkeypatch).get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert len(body["commit"]) == 40  # X-Agent 硬门槛：40 位 commit


def test_xagent_verification(monkeypatch) -> None:
    resp = _client(monkeypatch).get("/.well-known/xagent-verification.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"schemaVersion": 1, "slug": "yai-test", "commit": "0" * 40}


def test_tools_endpoint_lists_discovered_capabilities(monkeypatch) -> None:
    resp = _client(monkeypatch).get("/v1/tools")
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()]
    assert "list_notes" in names


def test_agent_run_endpoint_returns_events_and_text(monkeypatch) -> None:
    resp = _client(monkeypatch).post("/v1/agent/run", json={"task": "你好"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy"] == "direct"
    assert body["final_text"].startswith("你好")
    event_types = [e["type"] for e in body["events"]]
    assert "strategy_selected" in event_types
    assert event_types[-1] == "done"
