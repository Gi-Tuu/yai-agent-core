"""Sidecar 服务端：会话控制协议 + AgentCore 装配。

设计边界（重要）：本模块**不定义工具调用通道**。宿主工具必须经 MCP / OpenAPI
暴露，sidecar 作为客户端接入；协议里只有 `agent/run`、`agent/cancel`、
`approval/request`、`clarify/request` 这类会话控制消息。这样不会与 MCP 重复，
也让任何语言的宿主都能复用同一套工具适配器。

进程模型：一个 sidecar 进程同一时刻只跑一个任务（与 ``AgentCore`` 的串行锁
一致）；不做持久化/断点恢复，重启即丢内存状态。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from yai_core.core import AgentCore
from yai_core.policy import AllowlistPolicy
from yai_core.sidecar.channel import DEFAULT_INTERACTION_TIMEOUT, SidecarChannel
from yai_core.sidecar.protocol import (
    ALREADY_INITIALIZED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    METHOD_NOT_FOUND,
    NOT_INITIALIZED,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    RUN_BUSY,
    SHUTTING_DOWN,
    RpcError,
    encode,
    error,
    jsonable,
    notification,
    response,
)
from yai_core.sidecar.protocol import (
    request as rpc_request,
)
from yai_core.types import Subject

Send = Callable[[dict[str, Any]], Awaitable[None]]
ModelFactory = Callable[[dict[str, Any]], Any]
ToolAttacher = Callable[[AgentCore, dict[str, Any]], Awaitable[list[Any]]]


def _build_openai_model(config: dict[str, Any]) -> Any:
    """默认模型工厂：OpenAI 兼容后端（需要 ``[llm]`` extra）。"""
    provider = str(config.get("provider", "openai") or "openai")
    if provider not in ("openai", "openai-compatible", "openai_compatible"):
        raise ValueError(
            "model.provider 只支持 openai-compatible（其它模型请走自定义 model_factory）"
        )
    from yai_core.llm.openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(
        api_key=config.get("api_key"),
        base_url=config.get("base_url"),
        model=config.get("model"),
        strong_model=config.get("strong_model"),
        timeout=config.get("timeout"),
    )


def _build_policy(config: Any) -> AllowlistPolicy:
    if config is None:
        return AllowlistPolicy((), mode="auto")
    if not isinstance(config, dict):
        raise ValueError("policy 必须是 object")
    mode = str(config.get("mode", "auto"))
    allow = config.get("allow") or []
    if not isinstance(allow, (list, tuple)) or not all(
        isinstance(name, str) and name.strip() for name in allow
    ):
        raise ValueError("policy.allow 必须是非空字符串数组")
    return AllowlistPolicy(tuple(name.strip() for name in allow), mode=mode)


async def attach_configured_tools(
    core: AgentCore, params: dict[str, Any]
) -> list[Any]:
    """按 initialize 参数接入 MCP / OpenAPI；返回需要关闭的桥接对象。"""
    bridges: list[Any] = []

    mcp_items = params.get("mcp") or []
    openapi_items = params.get("openapi") or []
    if not isinstance(mcp_items, list) or not isinstance(openapi_items, list):
        raise ValueError("mcp / openapi 必须是数组")

    try:
        if mcp_items:
            try:
                from yai_core.integrations.mcp import (
                    McpServerConfig,
                    attach_mcp_tools,
                )
            except ImportError as exc:
                raise ValueError(
                    "接入 MCP 需要可选依赖：pip install 'yai-agent-core[mcp]'"
                ) from exc
            for item in mcp_items:
                if not isinstance(item, dict):
                    raise ValueError("mcp 数组元素必须是 object")
                config = McpServerConfig(
                    alias=str(item.get("alias", "host")),
                    url=item.get("url"),
                    command=item.get("command"),
                    args=list(item.get("args") or []),
                    env=item.get("env"),
                    prefix=str(item.get("prefix", "")),
                )
                bridges.append(await attach_mcp_tools(core.registry, config))

        if openapi_items:
            try:
                from yai_core.integrations.openapi import (
                    OpenApiSpecConfig,
                    attach_openapi_tools,
                )
            except ImportError as exc:
                raise ValueError(
                    "接入 OpenAPI 需要可选依赖：pip install 'yai-agent-core[openapi]'"
                ) from exc
            for item in openapi_items:
                if not isinstance(item, dict):
                    raise ValueError("openapi 数组元素必须是 object")
                openapi_config = OpenApiSpecConfig(
                    alias=str(item.get("alias", "rest")),
                    url=item.get("url"),
                    path=item.get("path"),
                    spec=item.get("spec"),
                    prefix=str(item.get("prefix", "")),
                    base_url_override=item.get("base_url_override"),
                    read_only=bool(item.get("read_only", False)),
                    max_operations=int(item.get("max_operations", 40)),
                    include_paths=tuple(item.get("include_paths") or ()),
                    exclude_paths=tuple(item.get("exclude_paths") or ()),
                    timeout=float(item.get("timeout", 15.0)),
                )
                bridges.append(
                    await attach_openapi_tools(core.registry, openapi_config)
                )
    except BaseException:
        # 第二个桥接失败时，第一个已经拉起的子进程/HTTP 客户端不能留。
        for bridge in reversed(bridges):
            with contextlib.suppress(Exception):
                closer = getattr(bridge, "aclose", None)
                if callable(closer):
                    await closer()
        raise

    return bridges


class SidecarServer:
    """JSON-RPC 会话服务；可由 stdio 驱动，也可在测试里直接喂消息。"""

    def __init__(
        self,
        *,
        send: Send | None = None,
        model_factory: ModelFactory | None = None,
        tool_attacher: ToolAttacher | None = None,
    ) -> None:
        self._send = send or self._stdio_send
        self._model_factory = model_factory or _build_openai_model
        self._tool_attacher = tool_attacher or attach_configured_tools
        self._core: AgentCore | None = None
        self._bridges: list[Any] = []
        self._channel: SidecarChannel | None = None
        self._initialized = False
        self._closed = False
        self._stopping = False
        self._active_run: asyncio.Task[None] | None = None
        self._active_run_id: str | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._server_seq = 0

    # ------------------------------------------------------------------ 入口

    async def handle(self, message: Any) -> None:
        """处理一条已解析的 JSON-RPC 消息（入站请求或对端响应）。"""
        if not isinstance(message, dict):
            await self._send(error(None, RpcError(INVALID_REQUEST, "消息必须是 object")))
            return
        if message.get("jsonrpc") != JSONRPC_VERSION:
            await self._send(
                error(
                    message.get("id"),
                    RpcError(INVALID_REQUEST, "jsonrpc 必须是 \"2.0\""),
                )
            )
            return

        if "method" in message:
            await self._handle_incoming_request(message)
            return

        request_id = message.get("id")
        if request_id is None or not ("result" in message or "error" in message):
            await self._send(
                error(request_id, RpcError(INVALID_REQUEST, "不是合法的请求或响应"))
            )
            return
        future = self._pending.pop(str(request_id), None)
        if future is None or future.done():
            return
        if "error" in message:
            payload = message["error"] if isinstance(message["error"], dict) else {}
            future.set_exception(
                RpcError(
                    int(payload.get("code", INTERNAL_ERROR)),
                    str(payload.get("message", "宿主返回错误")),
                    payload.get("data"),
                )
            )
        else:
            future.set_result(message.get("result"))

    async def handle_line(self, line: bytes | str) -> None:
        """解析一行 NDJSON 再交给 :meth:`handle`；解析失败回 PARSE_ERROR。"""
        try:
            text = line.decode("utf-8") if isinstance(line, bytes) else line
            message = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            await self._send(error(None, RpcError(PARSE_ERROR, "无法解析 JSON 行")))
            return
        await self.handle(message)

    async def serve_stdio(self) -> None:
        """从 stdin 逐行读、向 stdout 逐行写；EOF 或 shutdown 后收尾。"""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def _reader() -> None:
            try:
                for line in sys.stdin.buffer:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
            except Exception:  # noqa: BLE001 - 读线程失败按 EOF 收尾
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=_reader, name="yai-sidecar-stdin", daemon=True).start()
        try:
            while not self._stopping:
                line = await queue.get()
                if line is None:
                    break
                if not line.strip():
                    continue
                await self.handle_line(line)
        finally:
            await self._cleanup()

    # -------------------------------------------------------------- 消息分发

    async def _handle_incoming_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if params is None:
            params = {}
        has_id = request_id is not None
        try:
            if not isinstance(method, str):
                raise RpcError(INVALID_REQUEST, "method 必须是字符串")
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params 必须是 object")
            if method == "initialize":
                result = await self._initialize(params)
            elif method == "ping":
                result = {"pong": True, "protocol_version": PROTOCOL_VERSION}
            elif method == "shutdown":
                result = await self._shutdown()
            elif not self._initialized:
                raise RpcError(NOT_INITIALIZED, "请先调用 initialize")
            elif method == "agent/run":
                result = await self._start_run(params)
            elif method == "agent/cancel":
                result = await self._cancel_run(params)
            else:
                raise RpcError(METHOD_NOT_FOUND, f"未知方法：{method}")
        except RpcError as exc:
            if has_id:
                await self._send(error(request_id, exc))
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 协议层兜底，绝不把异常穿给宿主
            if has_id:
                await self._send(
                    error(
                        request_id,
                        RpcError(
                            INTERNAL_ERROR,
                            f"内部错误：{type(exc).__name__}",
                        ),
                    )
                )
            return
        if has_id:
            await self._send(response(request_id, result))

    # ---------------------------------------------------------------- 生命周期

    async def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._initialized:
            raise RpcError(ALREADY_INITIALIZED, "initialize 只能调用一次")
        if self._closed:
            raise RpcError(SHUTTING_DOWN, "sidecar 已关闭")

        model_config = params.get("model")
        if model_config is None:
            model_config = {}
        if not isinstance(model_config, dict):
            raise RpcError(INVALID_PARAMS, "model 必须是 object")
        try:
            model = self._model_factory(model_config)
        except ImportError as exc:
            raise RpcError(
                INVALID_PARAMS,
                "模型后端缺少可选依赖",
                {"hint": str(exc)},
            ) from exc
        except ValueError as exc:
            raise RpcError(INVALID_PARAMS, str(exc)) from exc

        try:
            timeout = float(params.get("interaction_timeout", DEFAULT_INTERACTION_TIMEOUT))
        except (TypeError, ValueError) as exc:
            raise RpcError(INVALID_PARAMS, "interaction_timeout 必须是数字") from exc
        if timeout <= 0:
            raise RpcError(INVALID_PARAMS, "interaction_timeout 必须大于 0")

        try:
            policy = _build_policy(params.get("policy"))
        except ValueError as exc:
            raise RpcError(INVALID_PARAMS, str(exc)) from exc

        self._channel = SidecarChannel(self._request_host, timeout=timeout)
        try:
            core = AgentCore(
                model,
                channel=self._channel,
                policy=policy,
                llm_router=params.get("llm_router", "auto"),
                composition=bool(params.get("composition", False)),
                strict_direct=bool(params.get("strict_direct", False)),
                max_iters=int(params.get("max_iters", 6)),
                tool_result_char_limit=params.get("tool_result_char_limit", 16_000),
                context_compaction=bool(params.get("context_compaction", False)),
                model_timeout=params.get("model_timeout", 60.0),
                tool_timeout=params.get("tool_timeout"),
            )
        except (TypeError, ValueError) as exc:
            raise RpcError(INVALID_PARAMS, f"AgentCore 配置不合法：{exc}") from exc

        try:
            self._bridges = await self._tool_attacher(core, params)
        except (ValueError, ImportError) as exc:
            await core.aclose()
            raise RpcError(INVALID_PARAMS, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 工具接入失败要回错并释放
            await core.aclose()
            raise RpcError(
                INVALID_PARAMS,
                f"工具接入失败：{type(exc).__name__}",
            ) from exc

        self._core = core
        self._initialized = True
        return {
            "protocol_version": PROTOCOL_VERSION,
            "kernel_version": _kernel_version(),
            "tools": core.list_tools(),
            "capabilities": {
                "streaming": True,
                "approval": True,
                "clarify": True,
                "single_run": True,
                "tool_transports": ["mcp", "openapi"],
            },
        }

    async def _shutdown(self) -> dict[str, Any]:
        await self._cleanup()
        self._stopping = True
        return {"stopped": True}

    async def _cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._active_run
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._active_run = None
        self._active_run_id = None
        for bridge in reversed(self._bridges):
            with contextlib.suppress(Exception):
                closer = getattr(bridge, "aclose", None)
                if callable(closer):
                    await closer()
        self._bridges = []
        if self._core is not None:
            with contextlib.suppress(Exception):
                await self._core.aclose()
            self._core = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RpcError(SHUTTING_DOWN, "sidecar 正在关闭"))
        self._pending.clear()

    # ------------------------------------------------------------------ 运行

    async def _start_run(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._stopping or self._closed:
            raise RpcError(SHUTTING_DOWN, "sidecar 正在关闭")
        if self._core is None:
            raise RpcError(NOT_INITIALIZED, "请先调用 initialize")
        if self._active_run is not None and not self._active_run.done():
            raise RpcError(RUN_BUSY, "已有任务在运行；请先取消或等待完成")
        task_text = params.get("task")
        if not isinstance(task_text, str) or not task_text.strip():
            raise RpcError(INVALID_PARAMS, "task 必须是非空字符串")
        subject = _build_subject(params.get("subject"))

        run_id = uuid.uuid4().hex[:12]
        self._active_run_id = run_id
        self._active_run = asyncio.create_task(
            self._execute_run(run_id, task_text.strip(), subject),
            name=f"yai-sidecar-run-{run_id}",
        )
        return {"run_id": run_id}

    async def _cancel_run(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = params.get("run_id")
        task = self._active_run
        if task is None or task.done() or run_id != self._active_run_id:
            return {"cancelled": False, "run_id": run_id}
        task.cancel()
        return {"cancelled": True, "run_id": run_id}

    async def _execute_run(
        self, run_id: str, task: str, subject: Subject | None
    ) -> None:
        assert self._core is not None
        seq = 0
        try:
            async for event in self._core.astream(task, subject=subject):
                await self._send(
                    notification(
                        "agent/event",
                        {
                            "run_id": run_id,
                            "seq": seq,
                            "type": event.type.value,
                            "data": jsonable(event.data),
                        },
                    )
                )
                seq += 1
        except asyncio.CancelledError:
            await self._send(
                notification("agent/cancelled", {"run_id": run_id, "reason": "cancelled"})
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 运行失败以通知返回，不杀进程
            await self._send(
                notification(
                    "agent/error",
                    {
                        "run_id": run_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            )
        finally:
            if self._active_run_id == run_id:
                self._active_run = None
                self._active_run_id = None

    # ------------------------------------------------------ 审批 / 澄清（反向请求）

    async def _request_host(self, method: str, params: dict[str, Any]) -> Any:
        if self._stopping or self._closed:
            return None
        self._server_seq += 1
        request_id = f"srv-{self._server_seq}"
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(rpc_request(request_id, method, params))
            timeout = self._channel.timeout if self._channel else 600.0
            try:
                async with asyncio.timeout(timeout):
                    return await future
            except TimeoutError:
                return None
            except RpcError:
                return None
        finally:
            self._pending.pop(request_id, None)

    # ------------------------------------------------------------------ 输出

    async def _stdio_send(self, message: dict[str, Any]) -> None:
        stream = sys.stdout.buffer
        stream.write(encode(message) + b"\n")
        stream.flush()


def _build_subject(value: Any) -> Subject | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RpcError(INVALID_PARAMS, "subject 必须是 object 或 null")
    subject_id = value.get("subject_id")
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise RpcError(INVALID_PARAMS, "subject.subject_id 必须是非空字符串")
    attributes = value.get("attributes") or {}
    if not isinstance(attributes, dict):
        raise RpcError(INVALID_PARAMS, "subject.attributes 必须是 object")
    return Subject(
        subject_id=subject_id.strip(),
        display_name=str(value.get("display_name", "")),
        attributes=attributes,
    )


def _kernel_version() -> str:
    from yai_core import __version__

    return __version__
