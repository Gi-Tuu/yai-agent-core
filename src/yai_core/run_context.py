"""Per-run / per-tool-call context for trusted host adapters.

These context variables are set by :class:`AgentCore` and :class:`ToolExecutor`
from values supplied by the host (never from model output). Integrations such as
the OpenAPI bridge read them to attach correlation headers to outbound calls.
"""

from __future__ import annotations

from contextlib import suppress
from contextvars import ContextVar

_RUN_ID: ContextVar[str | None] = ContextVar("yai_run_id", default=None)
_CALL_ID: ContextVar[str | None] = ContextVar("yai_call_id", default=None)


def current_run_id() -> str | None:
    return _RUN_ID.get()


def current_call_id() -> str | None:
    return _CALL_ID.get()


def set_run_id(run_id: str | None):
    return _RUN_ID.set(run_id)


def reset_run_id(token) -> None:
    # async generator 可能在不同 task/context 恢复；旧 token 只能忽略。
    with suppress(ValueError):
        _RUN_ID.reset(token)


def set_call_id(call_id: str | None):
    return _CALL_ID.set(call_id)


def reset_call_id(token) -> None:
    with suppress(ValueError):
        _CALL_ID.reset(token)
