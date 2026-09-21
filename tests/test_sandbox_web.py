"""host_g 极简网页壳（标准库 http.server）的离线测试。

在随机端口用线程起真实的 ThreadingHTTPServer，再用 urllib 走真实 HTTP 访问：
静态首页、健康检查、原生候选人数据，以及 POST /api/run 的"造工具→沙箱执行"
完整链路。全程离线，不依赖网络与 API Key。
"""

import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from host_g_sandbox import web_app  # noqa: E402


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web_app._Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post_json(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_index_html_served(server):
    status, body = _get(server, "/")
    assert status == 200
    assert "YAI Agent Core" in body
    assert "create_code_tool" in body


def test_health(server):
    status, body = _get(server, "/api/health")
    data = json.loads(body)
    assert status == 200 and data["ok"] is True


def test_native_candidates_api(server):
    status, body = _get(server, "/api/candidates")
    data = json.loads(body)
    assert status == 200
    names = [c["name"] for c in data["candidates"]]
    assert set(names) == {"林晓", "陈默", "周岚"}


def test_run_full_flow_creates_and_executes_code_tool(server):
    status, data = _post_json(server, "/api/run", {"task": "按技能0.6经验0.4排序"})
    assert status == 200

    events = data["events"]
    types = [e["type"] for e in events]
    assert "code_tool_created" in types

    calls = [e["data"]["tool"] for e in events if e["type"] == "tool_call"]
    assert calls == ["create_code_tool", "list_candidates", "weighted_score"]

    # weighted_score 的结果来自真实子进程沙箱。
    weighted = next(
        e
        for e in events
        if e["type"] == "tool_result" and e["data"].get("tool") == "weighted_score"
    )
    assert weighted["data"]["ok"] is True
    ranked = json.loads(weighted["data"]["preview"])
    assert [c["name"] for c in ranked] == ["林晓", "周岚", "陈默"]
    assert ranked[0]["score"] == 81.6

    assert "林晓" in data["final_text"]
    assert data["code_tools"]["live"] == 1


def test_run_defaults_task_when_empty(server):
    status, data = _post_json(server, "/api/run", {})
    assert status == 200 and data["events"]
    assert "技能" in data["task"]
