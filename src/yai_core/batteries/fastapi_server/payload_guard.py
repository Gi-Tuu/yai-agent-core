"""响应体大小守卫：给 /v1/agent/run 的 JSON 响应设上限（v0.7，纯标准库）。

威胁模型（如实声明）：防"长任务 / 失控循环产生超大响应"拖垮带宽与客户端解析，
**不改变结果语义**——只做有界裁剪并显式标记 truncated，绝不静默删改。

裁剪策略（对事件序列）：
- 保留"头 + 尾"，中间插入一个合成事件 _events_truncated 占位（说明省略条数）；
- final_text 超长则尾部截断并加省略号；
- 整体 JSON 仍超总字符上限时逐级收缩保留事件数，极端情况下只留占位。
"""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
from typing import Any

_ENABLED_ENV = "YAI_RESP_GUARD_ENABLED"
_MAX_EVENTS_ENV = "YAI_RESP_MAX_EVENTS"
_MAX_FINAL_ENV = "YAI_RESP_MAX_FINAL_CHARS"
_MAX_TOTAL_ENV = "YAI_RESP_MAX_TOTAL_CHARS"

#: 事件被裁剪后插入的合成事件类型（不是真实 EventType，仅出现在 HTTP 响应里）。
TRUNC_MARKER_TYPE = "_events_truncated"


def _truthy(raw: str | None) -> bool:
    """enabled 解析：未设置视为开启；显式 0/false/no/off 才关闭。"""
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _pos_int(raw: str | None, default: int) -> int:
    """正整数解析：未设置回退默认；非法/非正回退默认 + warning。"""
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        warnings.warn(f"响应守卫配置 {raw!r} 非法，回退默认值 {default}", stacklevel=2)
        return default
    if value <= 0:
        warnings.warn("响应守卫配置必须为正整数，回退默认值 " + str(default), stacklevel=2)
        return default
    return value


@dataclass
class ResponseGuardConfig:
    """响应守卫配置；默认值是对外承诺，正常小响应不受任何影响。"""

    enabled: bool = True
    max_events: int = 200
    max_final_chars: int = 8000
    max_total_chars: int = 256_000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ResponseGuardConfig:
        """从环境变量构造；env=None 读真实 os.environ，测试注入假字典。"""
        environ = env if env is not None else os.environ
        return cls(
            enabled=_truthy(environ.get(_ENABLED_ENV)),
            max_events=_pos_int(environ.get(_MAX_EVENTS_ENV), 200),
            max_final_chars=_pos_int(environ.get(_MAX_FINAL_ENV), 8000),
            max_total_chars=_pos_int(environ.get(_MAX_TOTAL_ENV), 256_000),
        )


def _marker(omitted: int) -> dict[str, Any]:
    """合成占位事件：记录被省略的真实事件数。"""
    return {"type": TRUNC_MARKER_TYPE, "data": {"omitted": omitted}}


def _head_tail(events: list, keep: int) -> list:
    """保留"头 + 尾"共 keep 个位置（其中 1 个是占位）；keep >= 2。"""
    head = max(1, (keep - 1) // 2)
    tail = keep - 1 - head
    total = len(events)
    return events[:head] + [_marker(total - head - tail)] + events[total - tail:]


def bound_payload(payload: dict, config: ResponseGuardConfig) -> dict:
    """把 run 响应裁剪到配置上限内；返回新 dict，并附 truncated / events_total 标记。"""
    events = list(payload.get("events", []))
    total = len(events)
    out: dict[str, Any] = dict(payload)
    truncated = False

    final_text = out.get("final_text")
    if isinstance(final_text, str) and len(final_text) > config.max_final_chars:
        out["final_text"] = final_text[: config.max_final_chars] + "…"
        truncated = True

    kept = events
    if total > config.max_events:
        kept = _head_tail(events, config.max_events)
        truncated = True
    out["events"] = kept

    # 整体仍超总字符：逐级把保留事件数减半；降到 1 时只留占位，保证响应有界。
    keep = len(out["events"])
    while len(json.dumps(out, ensure_ascii=False)) > config.max_total_chars and keep > 1:
        keep = max(1, keep // 2)
        out["events"] = [_marker(total)] if keep <= 1 else _head_tail(events, keep)
        truncated = True

    out["events_total"] = total
    out["truncated"] = truncated
    return out
