"""FastAPI Battery：把内嵌 Core 暴露为在线 API。

内置两个部署验证端点：
- GET /health                                 -> {"status":"ok","commit":"<40位>"}
- GET /.well-known/yai-verification.json   -> {"schemaVersion":1,"slug":...,"commit":...}

注意：本模块属于可选 Battery，允许依赖 fastapi/pydantic；yai_core 内核仍零硬依赖。
"""

from __future__ import annotations

import os
from typing import Any

try:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, PlainTextResponse
    from pydantic import BaseModel
except ImportError as exc:  # pragma: no cover - 仅在缺少可选依赖时触发
    raise ImportError(
        "FastAPI Battery 需要可选依赖：uv pip install -e '.[server]'"
    ) from exc

from yai_core.batteries.fastapi_server.auth import ApiKeyAuth
from yai_core.batteries.fastapi_server.payload_guard import (
    ResponseGuardConfig,
    bound_payload,
)
from yai_core.batteries.fastapi_server.ratelimit import (
    RateLimitConfig,
    SlidingWindowLimiter,
    client_ip,
)
from yai_core.batteries.fastapi_server.readiness import (
    DEFAULT_PROBE_TIMEOUT,
    default_probes,
    run_probes,
)

# 限流只拦这一条 POST：三个 GET 端点（health / verification / tools）是
# 探活与能力清单端点：任何限流状态下都必须无条件 200。
_LIMITED_METHOD = "POST"
_LIMITED_PATHS = frozenset({"/v1/agent/run"})


class RunRequest(BaseModel):
    task: str


def create_app(
    core: Any,
    lifespan: Any = None,
    *,
    auth: ApiKeyAuth | None = None,
    rate_limit: RateLimitConfig | None = None,
    guard: ResponseGuardConfig | None = None,
    readiness_probes: dict | None = None,
    readiness_timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> FastAPI:
    app = FastAPI(title="YAI Agent Core API", version="0.1.0", lifespan=lifespan)
    commit = os.getenv("YAI_GIT_COMMIT", "dev")
    slug = os.getenv("YAI_PROJECT_SLUG", "yai-agent-core")

    # 就绪探针：默认只做廉价本地状态报告（不调模型、不发网络请求），宿主可追加
    # DB ping / 模型探活等；同名探针以宿主注册的为准。
    probes = {**default_probes(core), **(readiness_probes or {})}
    # /metrics 默认在配置了 API Key 时一并保护；YAI_METRICS_PUBLIC=1 可显式公开。
    # /health 与 /readyz 恒公开（探针高频抓取，绝不能被鉴权 / 限流误伤）。
    _metrics_public = os.getenv("YAI_METRICS_PUBLIC", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    # 公网演示限流（opt-out via env）：None -> 从环境变量读默认策略；
    # enabled=False 时不注册中间件，零开销、零行为变化。
    config = rate_limit if rate_limit is not None else RateLimitConfig.from_env()
    # 响应体大小守卫（opt-out via env）：默认开启，正常小响应不受影响。
    guard_config = guard if guard is not None else ResponseGuardConfig.from_env()
    # API Key 鉴权（opt-in via env）：未配置 Key 时关闭、全公开，本地/演示零门槛。
    auth_cfg = auth if auth is not None else ApiKeyAuth.from_env()
    # 中间件注册顺序很关键：Starlette / FastAPI 里**后注册的中间件处在更外层**
    # （请求先经过它）。必须让限流位于最外层，使鉴权失败的请求也计入限流——
    # 否则未带 / 错误 Key 的请求在鉴权层直接 401、从不进入 limiter.check()，
    # API Key 就能被无限速在线爆破。因此这里**先注册鉴权（内层），再注册限流
    # （外层）**。

    # 配置了 Key 时，/metrics 默认也要鉴权（除非显式 YAI_METRICS_PUBLIC=1）。
    metrics_protected = auth_cfg.enabled and not _metrics_public

    # 1) 鉴权（内层，opt-in）：未配置 Key 时不注册中间件，零开销、行为不变。
    if auth_cfg.enabled:

        @app.middleware("http")
        async def _auth_middleware(request, call_next):
            path = request.url.path
            need_auth = auth_cfg.should_protect(path) or (
                path == "/metrics" and metrics_protected
            )
            if need_auth and not auth_cfg.check_header(
                request.headers.get("authorization"),
                request.headers.get("x-api-key"),
            ):
                # 未授权在中间件层直接 401：不进 core，不花模型钱。
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "unauthorized",
                        "detail": "缺少或无效的 API Key（Authorization: Bearer <key>）",
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return await call_next(request)

    # 2) 限流（外层）：最后注册 => 最外层。所有 POST /v1/agent/run（含未授权
    #    请求）都先计数，超限直接 429，连鉴权与 core 都不再进入。
    #    只挂在这条 POST 白名单内：三个 GET 端点是探活硬门槛，
    #    全局兜底也**不许**扩到它们上面（否则访问者会看到 429）。
    if config.enabled:
        limiter = SlidingWindowLimiter(config)
        # 全局兜底（默认关闭）：跨所有来源的总配额，进程内计数。
        global_limiter = (
            SlidingWindowLimiter(config, per_minute=config.global_per_minute)
            if config.global_per_minute > 0
            else None
        )

        @app.middleware("http")
        async def _rate_limit_middleware(request, call_next):
            if request.method == _LIMITED_METHOD and request.url.path in _LIMITED_PATHS:
                key = client_ip(
                    request.headers.get("x-forwarded-for"),
                    request.client.host if request.client else None,
                )
                # 先算全局再算每 IP：全局拒绝时不消耗该 IP 的配额。反向的代价是
                # "全局放行但该 IP 超限"时白扣一个全局名额——宁可多拦不可漏放，
                # 且这条路径只在总配额真的快耗尽时才发生。
                scope = "global"
                allowed, retry_after = (
                    global_limiter.check("__global__")
                    if global_limiter is not None
                    else (True, 0)
                )
                if allowed:
                    scope = "ip"
                    allowed, retry_after = limiter.check(key)
                if not allowed:
                    # 被限请求在最外层直接返回：不进鉴权 / core，一分模型钱都不花。
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": "rate_limited",
                            "detail": (
                                "服务总请求量已达上限，请稍后重试"
                                if scope == "global"
                                else "请求过于频繁，请稍后重试"
                            ),
                            "scope": scope,
                            "retry_after": retry_after,
                        },
                        headers={"Retry-After": str(retry_after)},
                    )
            return await call_next(request)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "commit": commit}

    @app.get("/.well-known/yai-verification.json")
    async def verification() -> dict:
        return {"schemaVersion": 1, "slug": slug, "commit": commit}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """就绪探针：任一探针失败 / 超时即 503（编排系统据此摘流量，不重启）。"""
        ready, checks = await run_probes(probes, timeout=readiness_timeout)
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    @app.get("/metrics")
    async def metrics(format: str | None = None):
        """运行指标：默认 Prometheus 文本；?format=json 返回结构化快照。"""
        if format == "json":
            return JSONResponse(core.metrics_snapshot())
        return PlainTextResponse(
            core.metrics.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/v1/tools")
    async def list_tools() -> list[dict]:
        return core.list_tools()

    @app.post("/v1/agent/run")
    async def run_agent(req: RunRequest) -> dict:
        result = await core.run(req.task)
        payload = {
            "strategy": result.strategy.value,
            "final_text": result.final_text,
            "events": [
                {"type": e.type.value, "data": _jsonable(e.data)}
                for e in result.events
            ],
        }
        # 守卫默认开启：超上限的事件/正文被有界裁剪并标记 truncated。
        return bound_payload(payload, guard_config) if guard_config.enabled else payload

    return app


def _jsonable(data: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in data.items():
        out[k] = v if isinstance(v, str | int | float | bool | list | dict | None) else str(v)
    return out
