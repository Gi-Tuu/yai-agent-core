"""FastAPI Battery：把内嵌 Core 暴露为在线 API。

内置 X-Agent AI MCP Hackathon 要求的两个验证端点：
- GET /health                                 -> {"status":"ok","commit":"<40位>"}
- GET /.well-known/xagent-verification.json   -> {"schemaVersion":1,"slug":...,"commit":...}

注意：本模块属于可选 Battery，允许依赖 fastapi/pydantic；yai_core 内核仍零硬依赖。
"""

from __future__ import annotations

import os
from typing import Any

try:
    from fastapi import FastAPI
    from pydantic import BaseModel
except ImportError as exc:  # pragma: no cover - 仅在缺少可选依赖时触发
    raise ImportError(
        "FastAPI Battery 需要可选依赖：uv pip install -e '.[server]'"
    ) from exc


class RunRequest(BaseModel):
    task: str


def create_app(core: Any, lifespan: Any = None) -> FastAPI:
    app = FastAPI(title="YAI Agent Core API", version="0.1.0", lifespan=lifespan)
    commit = os.getenv("YAI_GIT_COMMIT", "dev")
    slug = os.getenv("YAI_PROJECT_SLUG", "yai-agent-core")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "commit": commit}

    @app.get("/.well-known/xagent-verification.json")
    async def verification() -> dict:
        return {"schemaVersion": 1, "slug": slug, "commit": commit}

    @app.get("/v1/tools")
    async def list_tools() -> list[dict]:
        return core.list_tools()

    @app.post("/v1/agent/run")
    async def run_agent(req: RunRequest) -> dict:
        result = await core.run(req.task)
        return {
            "strategy": result.strategy.value,
            "final_text": result.final_text,
            "events": [
                {"type": e.type.value, "data": _jsonable(e.data)} for e in result.events
            ],
        }

    return app


def _jsonable(data: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in data.items():
        out[k] = v if isinstance(v, str | int | float | bool | list | dict | None) else str(v)
    return out
