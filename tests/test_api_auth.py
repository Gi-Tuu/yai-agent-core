"""API Key 鉴权测试：纯函数 + FastAPI TestClient 端到端，全程离线。"""

from __future__ import annotations

from starlette.testclient import TestClient

from yai_core.batteries.fastapi_server.app import create_app
from yai_core.batteries.fastapi_server.auth import ApiKeyAuth
from yai_core.batteries.fastapi_server.ratelimit import RateLimitConfig


class _Strat:
    def __init__(self, value: str) -> None:
        self.value = value


class _Result:
    strategy = _Strat("direct")
    final_text = "done"
    events = []


class _Core:
    async def run(self, task: str) -> _Result:
        return _Result()

    def list_tools(self) -> list:
        return []


def _client(auth: ApiKeyAuth) -> TestClient:
    # 显式关掉限流，避免鉴权测试受每 IP 配额干扰。
    app = create_app(
        _Core(), auth=auth, rate_limit=RateLimitConfig(enabled=False)
    )
    return TestClient(app)


def test_disabled_by_default() -> None:
    assert ApiKeyAuth.from_env({}).enabled is False


def test_single_and_multiple_keys() -> None:
    assert ApiKeyAuth.from_env({"YAI_API_KEY": "one"}).keys == ("one",)
    assert ApiKeyAuth.from_env({"YAI_API_KEYS": "a, b ,c"}).keys == ("a", "b", "c")


def test_protected_and_public_paths() -> None:
    auth = ApiKeyAuth(("k",))
    assert auth.should_protect("/v1/tools")
    assert auth.should_protect("/v1/agent/run")
    assert not auth.should_protect("/health")
    assert not auth.should_protect("/.well-known/xagent-verification.json")
    assert not auth.should_protect("/other")


def test_check_header_variants() -> None:
    auth = ApiKeyAuth(("secret",))
    assert auth.check_header("Bearer secret", None)
    assert auth.check_header("secret", None)  # 容忍裸 key
    assert auth.check_header(None, "secret")  # X-API-Key
    assert not auth.check_header("Bearer wrong", None)
    assert not auth.check_header(None, None)
    assert not auth.check_header("Bearer", None)


def test_endpoint_requires_key_when_enabled() -> None:
    client = _client(ApiKeyAuth(("secret",)))
    # 评审硬门槛路径即使开启鉴权也公开
    assert client.get("/health").status_code == 200
    assert client.get("/.well-known/xagent-verification.json").status_code == 200
    # /v1/* 无 Key -> 401
    assert client.get("/v1/tools").status_code == 401
    assert client.post("/v1/agent/run", json={"task": "hi"}).status_code == 401
    # 错误 Key -> 401
    assert (
        client.post(
            "/v1/agent/run",
            json={"task": "hi"},
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )
    # 正确 Bearer / X-API-Key -> 200
    assert (
        client.post(
            "/v1/agent/run",
            json={"task": "hi"},
            headers={"Authorization": "Bearer secret"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/agent/run",
            json={"task": "hi"},
            headers={"X-API-Key": "secret"},
        ).status_code
        == 200
    )


def test_endpoints_public_when_no_key() -> None:
    client = _client(ApiKeyAuth(()))
    assert client.get("/v1/tools").status_code == 200
    assert client.post("/v1/agent/run", json={"task": "hi"}).status_code == 200
