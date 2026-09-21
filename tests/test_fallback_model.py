"""FallbackModelProvider 离线测试：免费模型高峰过载时的自动降级。

不发任何网络请求，用脚本化假模型验证：
1. 主模型健康时只用主模型；
2. 主模型 429/超时/空响应时自动降级到兜底模型；
3. 400/401 等致命错误立即上抛，不浪费兜底模型；
4. 断路器冷却：主模型失败后，冷却期内不再调用它；
5. 全部失败时抛出最后一个可重试错误。
"""

from __future__ import annotations

import asyncio

import pytest

from yai_core import FallbackModelProvider, ModelResponse


class _HTTPError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class FakeProvider:
    """脚本化模型：按脚本依次返回 ModelResponse 或抛异常，并记录调用次数。"""

    def __init__(self, name: str, script: list) -> None:
        self.model = name
        self.strong_model = name
        self._script = script
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):  # noqa: ANN001
        i = self.calls
        self.calls += 1
        item = self._script[min(i, len(self._script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _ok(text: str) -> ModelResponse:
    return ModelResponse(content=text)


def _run(provider, **kw):
    return asyncio.run(provider.achat([{"role": "user", "content": "hi"}], **kw))


def test_primary_healthy_uses_primary() -> None:
    primary = FakeProvider("primary", [_ok("A")])
    backup = FakeProvider("backup", [_ok("B")])
    chain = FallbackModelProvider([primary, backup])
    resp = _run(chain)
    assert resp.content == "A"
    assert primary.calls == 1
    assert backup.calls == 0
    assert chain.model == "primary"


def test_429_falls_back_to_backup() -> None:
    primary = FakeProvider("primary", [_HTTPError(429)])
    backup = FakeProvider("backup", [_ok("B")])
    chain = FallbackModelProvider([primary, backup])
    resp = _run(chain)
    assert resp.content == "B"
    assert primary.calls == 1
    assert backup.calls == 1
    assert chain.model == "backup"


def test_timeout_falls_back() -> None:
    primary = FakeProvider("primary", [TimeoutError()])
    backup = FakeProvider("backup", [_ok("B")])
    chain = FallbackModelProvider([primary, backup])
    assert _run(chain).content == "B"


def test_connection_error_falls_back() -> None:
    primary = FakeProvider("primary", [ConnectionError("reset")])
    backup = FakeProvider("backup", [_ok("B")])
    chain = FallbackModelProvider([primary, backup])
    assert _run(chain).content == "B"


def test_empty_response_falls_back() -> None:
    primary = FakeProvider("primary", [ModelResponse(content="", tool_calls=[])])
    backup = FakeProvider("backup", [_ok("B")])
    chain = FallbackModelProvider([primary, backup])
    assert _run(chain).content == "B"


def test_fatal_400_raises_immediately_without_fallback() -> None:
    primary = FakeProvider("primary", [_HTTPError(400)])
    backup = FakeProvider("backup", [_ok("B")])
    chain = FallbackModelProvider([primary, backup])
    with pytest.raises(_HTTPError) as exc:
        _run(chain)
    assert exc.value.status_code == 400
    assert backup.calls == 0  # 致命错误不降级


def test_auth_401_raises_immediately() -> None:
    primary = FakeProvider("primary", [_HTTPError(401)])
    backup = FakeProvider("backup", [_ok("B")])
    chain = FallbackModelProvider([primary, backup])
    with pytest.raises(_HTTPError):
        _run(chain)
    assert backup.calls == 0


def test_all_retryable_fail_raises_last() -> None:
    primary = FakeProvider("primary", [_HTTPError(429)])
    backup = FakeProvider("backup", [_HTTPError(503)])
    chain = FallbackModelProvider([primary, backup])
    with pytest.raises(_HTTPError) as exc:
        _run(chain)
    assert exc.value.status_code == 503


def test_circuit_breaker_skips_cooling_primary() -> None:
    # 主模型第一次失败、兜底成功；断路器冷却期内，第二次调用不应再碰主模型。
    primary = FakeProvider("primary", [_HTTPError(429), _ok("A-recovered")])
    backup = FakeProvider("backup", [_ok("B1"), _ok("B2")])
    chain = FallbackModelProvider([primary, backup], cooldown=300.0)
    assert _run(chain).content == "B1"
    assert _run(chain).content == "B2"
    assert primary.calls == 1  # 冷却期内第二次跳过主模型
    assert backup.calls == 2


def test_primary_recovers_after_cooldown() -> None:
    primary = FakeProvider("primary", [_HTTPError(429), _ok("A-recovered")])
    backup = FakeProvider("backup", [_ok("B1"), _ok("B2")])
    chain = FallbackModelProvider([primary, backup], cooldown=0.0)
    assert _run(chain).content == "B1"
    # cooldown=0：下一次调用重新试探主模型，主模型已恢复
    assert _run(chain).content == "A-recovered"
    assert primary.calls == 2


def test_live_router_marker_and_status() -> None:
    primary = FakeProvider("primary", [_ok("A")])
    backup = FakeProvider("backup", [_ok("B")])
    chain = FallbackModelProvider([primary, backup])
    assert chain.yai_live_router is True
    st = chain.status()
    assert {s["model"] for s in st} == {"primary", "backup"}
    assert all(s["healthy"] for s in st)


def test_empty_providers_raises() -> None:
    with pytest.raises(ValueError):
        FallbackModelProvider([])


def test_tool_calls_response_is_valid() -> None:
    from yai_core import ToolCallRequest

    primary = FakeProvider(
        "primary",
        [ModelResponse(content="", tool_calls=[ToolCallRequest(id="1", name="fn")])],
    )
    backup = FakeProvider("backup", [_ok("B")])
    chain = FallbackModelProvider([primary, backup])
    resp = _run(chain)
    assert resp.tool_calls and resp.tool_calls[0].name == "fn"
    assert backup.calls == 0  # 有工具调用不算空响应
