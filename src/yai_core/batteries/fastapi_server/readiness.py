"""就绪探针（readiness）：与 liveness 分离，供编排系统决定是否把流量打进来。

两种健康检查语义不同，不能混用：

- ``GET /health``（**liveness**）：进程还活着吗？失败会触发**重启 / 重建** Pod，
  因此必须宽松——只报告进程存活，绝不检查下游依赖，否则一次下游抖动会引发重启风暴。
- ``GET /readyz``（**readiness**）：现在能接请求吗？失败只会把实例**移出负载均衡**
  （不重启），因此可以检查数据库、外部模型、MCP / OpenAPI 依赖是否就绪。

本模块只做两件零依赖的事：

1. 定义**探针协议**与并发执行器（带超时、异常隔离，一个探针挂死不拖垮整个端点）；
2. 提供一组**默认探针**——只做廉价的本地状态报告（注册了多少工具、记忆后端类型、
   长记忆是否开启），**绝不调用模型、不发起网络请求、不花钱**。真正的下游探活
   （DB ping、模型端点、外部 MCP）由宿主经 ``create_app(readiness_probes=...)`` 注册。

探针返回值约定（同步或 async 均可）：

- ``None`` / ``True``：就绪，无附加详情；
- ``False``：未就绪，无详情；
- ``str``：未就绪，该字符串作为失败原因；
- ``dict``：``{"ok": bool, ...}``，``ok`` 缺省为 ``True``，其余键原样回显为详情；
- 抛异常：未就绪，详情为异常类型与消息。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

#: 单个探针允许的最长耗时（秒）；超时按未就绪计，避免探针挂死拖垮 /readyz。
DEFAULT_PROBE_TIMEOUT = 5.0

#: 探针：名字 -> 无参可调用（可 async）；返回值约定见模块 docstring。
Probe = Callable[[], Awaitable[Any] | Any]


def default_probes(core: Any) -> dict[str, Probe]:
    """构造默认就绪探针：只报告本地装配状态，恒为就绪、不触达任何外部依赖。"""

    def tools_probe() -> dict[str, Any]:
        # 0 个工具也是合法宿主（纯对话），故只报告、不判失败。
        return {"ok": True, "registered_tools": len(core.list_tools())}

    def memory_probe() -> dict[str, Any]:
        return {
            "ok": True,
            "backend": type(core.memory).__name__,
            "long_memory": getattr(core, "memory_vault", None) is not None,
        }

    return {"tools": tools_probe, "memory": memory_probe}


def _interpret(result: Any) -> dict[str, Any]:
    """把探针返回值归一化为 ``{"status": "ok"|"error", ...}``。"""
    if result is None or result is True:
        return {"status": "ok"}
    if result is False:
        return {"status": "error", "detail": "probe returned False"}
    if isinstance(result, str):
        return {"status": "error", "detail": result}
    if isinstance(result, dict):
        ok = bool(result.get("ok", True))
        detail = {k: v for k, v in result.items() if k != "ok"}
        if ok:
            return {"status": "ok", **detail}
        return {"status": "error", **detail}
    return {"status": "error", "detail": f"unsupported probe result: {type(result).__name__}"}


async def _run_one(name: str, probe: Probe, timeout: float) -> tuple[str, dict[str, Any]]:
    """执行单个探针，异常 / 超时都转成结构化失败结果（绝不向上抛）。"""
    try:
        outcome = probe()
        if inspect.isawaitable(outcome):
            async with asyncio.timeout(timeout):
                outcome = await outcome
        return name, _interpret(outcome)
    except TimeoutError:
        return name, {"status": "error", "detail": f"probe timed out after {timeout:g}s"}
    except Exception as exc:  # noqa: BLE001 - 探针失败只影响自身结果
        return name, {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}


async def run_probes(
    probes: dict[str, Probe], *, timeout: float = DEFAULT_PROBE_TIMEOUT
) -> tuple[bool, dict[str, dict[str, Any]]]:
    """并发执行全部探针，返回 ``(是否全部就绪, 各探针结果)``。"""
    if not probes:
        return True, {}
    results = await asyncio.gather(
        *(_run_one(name, probe, timeout) for name, probe in probes.items())
    )
    checks = {name: outcome for name, outcome in results}
    ready = all(outcome.get("status") == "ok" for outcome in checks.values())
    return ready, checks
