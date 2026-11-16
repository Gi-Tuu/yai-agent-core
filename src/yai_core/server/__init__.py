"""Production sidecar entrypoint for YAI Core.

This module is the installable process boundary used by host products (for
example the AMBRACE sidecar). It is intentionally separate from
``scripts/serve_example.py``: the latter is a demo host with example tools and
is not shipped in the wheel.

Environment:
    OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL
        model backend; required unless ``YAI_SIDECAR_OFFLINE=1``.
    OPENAPI_SPEC_URL / OPENAPI_BASE_URL / OPENAPI_AUTH_TOKEN
        optional AMBRACE/OpenAPI tool bridge.
    OPENAPI_ALLOW_WRITES
        default false; with false the OpenAPI bridge registers 0 tools.
    YAI_SERVICE_KEY
        trusted control-plane key for run_id/cancel/shutdown.
    YAI_DB_PATH
        optional SQLite path for history; otherwise in-memory.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

try:
    import uvicorn
except ImportError as exc:  # pragma: no cover - optional dependency
    raise ImportError(
        "yai_core.server 需要可选依赖：pip install 'yai-agent-core[server,openapi,llm]'"
    ) from exc

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from yai_core.batteries.fastapi_server import create_app
from yai_core.core import AgentCore
from yai_core.integrations.openapi import (
    attach_openapi_tools,
)
from yai_core.integrations.openapi import (
    config_from_env as openapi_config_from_env,
)
from yai_core.llm.openai_compat import OpenAICompatProvider
from yai_core.llm.scripted import ScriptedModel
from yai_core.memory import InMemoryStore, SqliteStore
from yai_core.types import ModelResponse

LOGGER = logging.getLogger("yai_core.server")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def build_model() -> Any:
    """Build the model from env; offline mode is for health/smoke tests only."""
    if _truthy(os.environ.get("YAI_SIDECAR_OFFLINE")):
        return ScriptedModel([ModelResponse(content="sidecar offline")])
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY 未设置；sidecar 需要模型配置。"
            "仅 smoke 测试可设置 YAI_SIDECAR_OFFLINE=1。"
        )
    return OpenAICompatProvider(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
        model=os.environ.get("LLM_MODEL"),
        max_retries=0,
        timeout=30.0,
    )


def _build_memory() -> Any:
    store = SqliteStore.from_env()
    return store if store is not None else InMemoryStore()


def build_app(
    *,
    model: Any | None = None,
    service_key: str | None = None,
) -> tuple[Any, Any]:
    """Return ``(app, core)`` for tests/embedding; no process is started."""
    model = model if model is not None else build_model()
    core = AgentCore(
        model,
        memory=_build_memory(),
        auto_approve_tools=True,
        tool_timeout=float(os.environ.get("YAI_TOOL_TIMEOUT", "60")),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        bridge = None
        cfg = openapi_config_from_env(alias="rest")
        if cfg is not None:
            if not _truthy(os.environ.get("OPENAPI_ALLOW_WRITES")):
                cfg.read_only = True
            try:
                bridge = await attach_openapi_tools(core.registry, cfg)
                LOGGER.info("OpenAPI mounted: %s tool(s)", len(bridge.specs))
            except Exception as exc:  # noqa: BLE001 - degrade, keep sidecar alive
                LOGGER.warning("OpenAPI mount failed: %s: %s", type(exc).__name__, exc)
        try:
            yield
        finally:
            if bridge is not None:
                with contextlib.suppress(Exception):
                    await bridge.aclose()

    app = create_app(
        core,
        lifespan=lifespan,
        service_key=service_key or os.environ.get("YAI_SERVICE_KEY") or None,
    )
    app.state.yai_server = None

    @app.post("/v1/shutdown")
    async def shutdown(request: Request) -> Any:
        # create_app already validates X-Service-Key for run_id paths; repeat
        # the check here because this route must never be callable anonymously.
        expected = service_key or os.environ.get("YAI_SERVICE_KEY") or ""
        presented = request.headers.get("x-service-key", "")
        if not expected or not hmac.compare_digest(presented, expected):
            raise HTTPException(status_code=403, detail="service key 无效或未配置")
        server = getattr(app.state, "yai_server", None)
        if server is None:
            raise HTTPException(status_code=503, detail="server 未以 sidecar 模式启动")
        server.should_exit = True
        return JSONResponse(status_code=202, content={"status": "shutting_down"})

    return app, core


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yai-core-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper())
    try:
        app, _core = build_app()
    except Exception as exc:  # noqa: BLE001 - startup failure should be explicit
        raise SystemExit(f"yai-core-server 启动失败：{exc}") from exc

    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=True,
    )
    server = uvicorn.Server(config)
    app.state.yai_server = server
    try:
        server.run()
    except KeyboardInterrupt:
        return 0
    return 0


__all__ = ["build_app", "build_model", "main"]
