"""限流 Battery 测试：滑动窗口、429 + Retry-After、GET 放行、假时钟全离线。

统一范式：TestClient + 计数版 ScriptedModel + 假时钟（now 列表），禁止 time.sleep。
"""

from __future__ import annotations

import warnings

from fastapi.testclient import TestClient

from yai_core import AgentCore, ModelResponse, build_spec
from yai_core.batteries.fastapi_server import RateLimitConfig, create_app
from yai_core.batteries.fastapi_server.auth import ApiKeyAuth
from yai_core.batteries.fastapi_server.ratelimit import SlidingWindowLimiter, client_ip


class CountingModel:
    """记录 achat 调用次数：断言被限请求一分模型钱都不花。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        return ModelResponse(content="好的")


def list_notes() -> list:
    """列出全部笔记。"""
    return [{"id": 1, "title": "Core 骨架"}]


class FakeClock:
    """可推进的假时钟（秒）；测试推进时间而不是 sleep 等真实窗口。"""

    def __init__(self) -> None:
        self.now = [0.0]

    def __call__(self) -> float:
        return self.now[0]

    def advance(self, seconds: float) -> None:
        self.now[0] += seconds


def make_client(model: CountingModel, config: RateLimitConfig | None) -> TestClient:
    core = AgentCore(model)
    core.register_tools([build_spec(list_notes)])
    return TestClient(create_app(core, rate_limit=config))


def limited_config(clock: FakeClock, per_minute: int = 2) -> RateLimitConfig:
    """小配额便于测试：per_minute 次 200，之后 429。"""
    return RateLimitConfig(
        enabled=True, per_minute=per_minute, window_seconds=60, time_func=clock
    )


# ---------- 1/2. 超限 429 + Retry-After；推进假时钟后恢复 ----------

def test_over_limit_returns_429_then_recovers(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    clock = FakeClock()
    model = CountingModel()
    client = make_client(model, limited_config(clock, per_minute=2))

    assert client.post("/v1/agent/run", json={"task": "1"}).status_code == 200
    assert client.post("/v1/agent/run", json={"task": "2"}).status_code == 200

    limited = client.post("/v1/agent/run", json={"task": "3"})
    assert limited.status_code == 429
    body = limited.json()
    assert body["error"] == "rate_limited"
    assert isinstance(body["retry_after"], int) and body["retry_after"] >= 1
    assert limited.headers["Retry-After"] == str(body["retry_after"])  # 同一计算源

    clock.advance(seconds=61)  # 越过窗口：滑动窗口语义下恢复
    assert client.post("/v1/agent/run", json={"task": "4"}).status_code == 200


# ---------- 3. 429 状态下三个 GET 端点仍 200（评审硬门槛） ----------

def test_get_endpoints_never_limited(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    monkeypatch.setenv("YAI_PROJECT_SLUG", "yai-test")
    clock = FakeClock()
    client = make_client(CountingModel(), limited_config(clock, per_minute=1))

    assert client.post("/v1/agent/run", json={"task": "1"}).status_code == 200
    assert client.post("/v1/agent/run", json={"task": "2"}).status_code == 429

    assert client.get("/health").status_code == 200
    assert client.get("/.well-known/xagent-verification.json").status_code == 200
    assert client.get("/v1/tools").status_code == 200


# ---------- 4. enabled=False 全放行 ----------

def test_disabled_config_allows_everything(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    clock = FakeClock()
    config = RateLimitConfig(
        enabled=False, per_minute=1, window_seconds=60, time_func=clock
    )
    client = make_client(CountingModel(), config)
    for _i in range(5):
        assert client.post("/v1/agent/run", json={"task": "x"}).status_code == 200


# ---------- 5. 每 IP 隔离 ----------

def test_per_ip_isolation(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    clock = FakeClock()
    client = make_client(CountingModel(), limited_config(clock, per_minute=1))

    ok_a = client.post(
        "/v1/agent/run", json={"task": "a"}, headers={"X-Forwarded-For": "1.1.1.1"}
    )
    ok_b = client.post(
        "/v1/agent/run", json={"task": "b"}, headers={"X-Forwarded-For": "2.2.2.2"}
    )
    limited_a = client.post(
        "/v1/agent/run", json={"task": "a2"}, headers={"X-Forwarded-For": "1.1.1.1"}
    )
    assert ok_a.status_code == 200
    assert ok_b.status_code == 200  # 另一个 IP 有独立配额
    assert limited_a.status_code == 429  # 同一 IP 超限


# ---------- 6. client_ip() 六种输入 ----------

def test_client_ip_inputs() -> None:
    assert client_ip(None, "10.0.0.1") == "10.0.0.1"  # 无 XFF：回退对端
    assert client_ip("1.2.3.4", "10.0.0.1") == "1.2.3.4"  # 单级
    assert client_ip("1.2.3.4, 5.6.7.8", "10.0.0.1") == "1.2.3.4"  # 多级取最左
    assert client_ip(" , 5.6.7.8", "10.0.0.1") == "5.6.7.8"  # 含空段：跳过取非空
    assert client_ip("  ,  ", "10.0.0.1") == "10.0.0.1"  # 全空：回退对端
    assert client_ip(None, None) == "unknown"  # 双缺省


# ---------- 7. 被限请求不触达模型 ----------

def test_limited_request_never_reaches_model(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    clock = FakeClock()
    model = CountingModel()
    client = make_client(model, limited_config(clock, per_minute=2))

    statuses = [
        client.post("/v1/agent/run", json={"task": str(i)}).status_code
        for i in range(4)
    ]
    assert statuses == [200, 200, 429, 429]
    assert model.calls == 2  # 4 次请求只调用 2 次模型


# ---------- 8. env 三态与非法值 ----------

def test_from_env_states(monkeypatch) -> None:
    monkeypatch.delenv("YAI_RATE_LIMIT_ENABLED", raising=False)
    monkeypatch.delenv("YAI_RATE_LIMIT_PER_MINUTE", raising=False)
    monkeypatch.delenv("YAI_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    config = RateLimitConfig.from_env()
    assert (config.enabled, config.per_minute, config.window_seconds) == (True, 30, 60)

    config = RateLimitConfig.from_env(
        env={
            "YAI_RATE_LIMIT_ENABLED": "0",
            "YAI_RATE_LIMIT_PER_MINUTE": "10",
            "YAI_RATE_LIMIT_WINDOW_SECONDS": "30",
        }
    )
    assert (config.enabled, config.per_minute, config.window_seconds) == (False, 10, 30)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config = RateLimitConfig.from_env(
            env={"YAI_RATE_LIMIT_PER_MINUTE": "-3", "YAI_RATE_LIMIT_WINDOW_SECONDS": "x"}
        )
        assert (config.enabled, config.per_minute, config.window_seconds) == (True, 30, 60)
        assert len(caught) == 2


# ---------- 9. 窗口边界：正好等于窗口的请求视为过期 ----------

def test_window_boundary_inclusive_expiry(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    clock = FakeClock()
    limiter = SlidingWindowLimiter(limited_config(clock, per_minute=1))

    allowed, _ = limiter.check("1.1.1.1")
    assert allowed is True
    clock.advance(seconds=60)  # 恰好等于窗口：<= 清除语义下视为过期
    allowed, retry_after = limiter.check("1.1.1.1")
    assert allowed is True

    # 超限瞬间 retry_after 至少为 1
    clock.now[0] = 60.5
    limiter.check("2.2.2.2")
    allowed, retry_after = limiter.check("2.2.2.2")
    assert allowed is False and retry_after >= 1


# ---------- 10. 默认开启不破坏既有零配置调用 ----------

def test_default_create_app_still_works(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    monkeypatch.delenv("YAI_RATE_LIMIT_ENABLED", raising=False)
    core = AgentCore(CountingModel())
    core.register_tools([build_spec(list_notes)])
    client = TestClient(create_app(core))  # 与 test_battery_api.py 同款零配置调用
    assert client.get("/health").status_code == 200
    assert client.post("/v1/agent/run", json={"task": "你好"}).status_code == 200


# ---------- 11. 鉴权失败请求也计入限流（防 API Key 在线爆破） ----------

def test_unauthorized_requests_are_rate_limited(monkeypatch) -> None:
    """回归 M2：限流必须在鉴权外层，错误 Key 的请求也要计数。

    修复前鉴权在外层，错误 Key 直接 401、不经过 limiter，可无限速爆破；
    修复后前 N 次 401（已计数），配额耗尽后第 N+1 次返回 429。
    """
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    clock = FakeClock()
    core = AgentCore(CountingModel())
    core.register_tools([build_spec(list_notes)])
    app = create_app(
        core,
        auth=ApiKeyAuth(("secret",)),
        rate_limit=limited_config(clock, per_minute=2),
    )
    client = TestClient(app)
    bad = {"Authorization": "Bearer wrong"}
    # 前两次：未过鉴权 => 401，但已被最外层限流计数。
    assert client.post("/v1/agent/run", json={"task": "x"}, headers=bad).status_code == 401
    assert client.post("/v1/agent/run", json={"task": "x"}, headers=bad).status_code == 401
    # 第三次：同一 IP 配额耗尽 => 429（修复前这里仍会是 401）。
    assert client.post("/v1/agent/run", json={"task": "x"}, headers=bad).status_code == 429


# ---------- 12. 全局兜底配额（跨 IP 总闸，默认关闭） ----------

def global_config(
    clock: FakeClock, *, per_ip: int = 50, global_per_minute: int
) -> RateLimitConfig:
    """每 IP 给足配额，只让全局闸成为瓶颈。"""
    return RateLimitConfig(
        enabled=True,
        per_minute=per_ip,
        window_seconds=60,
        global_per_minute=global_per_minute,
        time_func=clock,
    )


def test_global_backstop_fires_across_ips(monkeypatch) -> None:
    """存在意义本身：每个 IP 都没超限，加起来照样能把实例刷爆。"""
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    clock = FakeClock()
    client = make_client(CountingModel(), global_config(clock, global_per_minute=3))

    for i, ip in enumerate(["1.1.1.1", "2.2.2.2", "3.3.3.3"]):
        ok = client.post(
            "/v1/agent/run", json={"task": str(i)}, headers={"X-Forwarded-For": ip}
        )
        assert ok.status_code == 200  # 每个 IP 各自远未超限

    denied = client.post(
        "/v1/agent/run", json={"task": "x"}, headers={"X-Forwarded-For": "4.4.4.4"}
    )
    assert denied.status_code == 429
    body = denied.json()
    assert body["scope"] == "global"
    assert body["error"] == "rate_limited"
    assert isinstance(body["retry_after"], int) and body["retry_after"] >= 1
    assert denied.headers["Retry-After"] == str(body["retry_after"])

    clock.advance(seconds=61)  # 同一个滑动窗口语义，全局配额随窗口恢复
    assert (
        client.post(
            "/v1/agent/run", json={"task": "y"}, headers={"X-Forwarded-For": "5.5.5.5"}
        ).status_code
        == 200
    )


def test_global_gate_is_evaluated_before_ip_gate(monkeypatch) -> None:
    """两道闸同时可用时，先算全局：全局拒绝不该消耗某个 IP 的配额。"""
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    clock = FakeClock()
    client = make_client(
        CountingModel(), global_config(clock, per_ip=1, global_per_minute=1)
    )
    headers = {"X-Forwarded-For": "9.9.9.9"}
    assert client.post("/v1/agent/run", json={"task": "1"}, headers=headers).status_code == 200
    denied = client.post("/v1/agent/run", json={"task": "2"}, headers=headers)
    assert denied.status_code == 429
    assert denied.json()["scope"] == "global"  # 不是 "ip"：全局闸先命中


def test_global_denial_never_reaches_model(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    clock = FakeClock()
    model = CountingModel()
    client = make_client(model, global_config(clock, global_per_minute=2))
    statuses = [
        client.post(
            "/v1/agent/run", json={"task": str(i)}, headers={"X-Forwarded-For": f"{i}.0.0.1"}
        ).status_code
        for i in range(1, 5)
    ]
    assert statuses == [200, 200, 429, 429]
    assert model.calls == 2


def test_get_endpoints_never_hit_global_gate(monkeypatch) -> None:
    """红线：全局兜底也不许扩到三个 GET 验证端点上。"""
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    monkeypatch.setenv("YAI_PROJECT_SLUG", "yai-test")
    clock = FakeClock()
    client = make_client(CountingModel(), global_config(clock, global_per_minute=1))
    assert client.post("/v1/agent/run", json={"task": "1"}).status_code == 200
    assert client.post("/v1/agent/run", json={"task": "2"}).status_code == 429
    assert client.get("/health").status_code == 200
    assert client.get("/.well-known/xagent-verification.json").status_code == 200
    assert client.get("/v1/tools").status_code == 200


def test_global_backstop_off_by_default(monkeypatch) -> None:
    """默认 0 = 关闭：不配这一项时行为与旧版逐字节一致。"""
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    clock = FakeClock()
    client = make_client(CountingModel(), limited_config(clock, per_minute=100))
    for i in range(1, 30):  # 29 次跨 IP 请求，全部放行
        assert (
            client.post(
                "/v1/agent/run",
                json={"task": str(i)},
                headers={"X-Forwarded-For": f"10.0.0.{i}"},
            ).status_code
            == 200
        )


def test_from_env_global_parsing() -> None:
    def parsed(raw: str | None) -> int:
        env = {} if raw is None else {"YAI_RATE_LIMIT_GLOBAL_PER_MINUTE": raw}
        return RateLimitConfig.from_env(env=env).global_per_minute

    assert parsed(None) == 0
    assert parsed("0") == 0
    assert parsed("120") == 120
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert parsed("-5") == 0
        assert len(caught) == 1
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert parsed("abc") == 0
        assert len(caught) == 1
