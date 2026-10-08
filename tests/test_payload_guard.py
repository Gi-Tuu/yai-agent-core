"""响应体大小守卫测试：纯函数边界，全程离线、零网络。"""

from __future__ import annotations

import json

from yai_core.batteries.fastapi_server.payload_guard import (
    TRUNC_MARKER_TYPE,
    ResponseGuardConfig,
    bound_payload,
)


def test_small_payload_untouched() -> None:
    payload = {
        "strategy": "direct",
        "final_text": "ok",
        "events": [{"type": "done", "data": {}}],
    }
    out = bound_payload(payload, ResponseGuardConfig())
    assert out["truncated"] is False
    assert out["events_total"] == 1
    assert out["events"] == payload["events"]
    assert out["final_text"] == "ok"


def test_too_many_events_keeps_head_and_tail() -> None:
    cfg = ResponseGuardConfig(max_events=5)
    events = [{"type": "tool_result", "data": {"i": i}} for i in range(10)]
    out = bound_payload(
        {"strategy": "react", "final_text": "done", "events": events}, cfg
    )
    assert out["truncated"] is True
    assert out["events_total"] == 10
    assert len(out["events"]) == 5
    assert out["events"][0]["data"]["i"] == 0  # 头保留
    assert out["events"][-1]["data"]["i"] == 9  # 尾保留
    marker = next(e for e in out["events"] if e["type"] == TRUNC_MARKER_TYPE)
    # keep=5 -> head=2, tail=2 -> omitted=6
    assert marker["data"]["omitted"] == 6


def test_long_final_text_truncated() -> None:
    cfg = ResponseGuardConfig(max_final_chars=10)
    out = bound_payload({"final_text": "x" * 50, "events": []}, cfg)
    assert out["truncated"] is True
    assert out["final_text"] == "x" * 10 + "…"


def test_oversized_events_shrink_to_total_budget() -> None:
    big = "y" * 50_000
    payload = {
        "final_text": "done",
        "events": [
            {"type": "tool_result", "data": {"blob": big}} for _ in range(3)
        ],
    }
    cfg = ResponseGuardConfig(max_total_chars=20_000)
    out = bound_payload(payload, cfg)
    assert out["truncated"] is True
    assert len(json.dumps(out, ensure_ascii=False)) <= cfg.max_total_chars


def test_guard_config_from_env() -> None:
    assert ResponseGuardConfig.from_env({"YAI_RESP_GUARD_ENABLED": "off"}).enabled is False
    cfg = ResponseGuardConfig.from_env(
        {"YAI_RESP_MAX_EVENTS": "7", "YAI_RESP_MAX_TOTAL_CHARS": "junk"}
    )
    assert cfg.max_events == 7
    assert cfg.max_total_chars == 256_000  # 非法值回退默认
