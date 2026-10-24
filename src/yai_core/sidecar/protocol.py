"""Sidecar 会话控制协议：stdio 上的 NDJSON + JSON-RPC 2.0。

这一层**只负责会话**：初始化、跑任务、取消、审批/澄清应答、停机。
工具调用不在本协议里——宿主工具必须经 MCP / OpenAPI 暴露，sidecar 作为
客户端去接；否则就成了“简化版 MCP”，与项目的多语言接入边界冲突。

一行一个 JSON 对象；日志与诊断只走 stderr。所有出站对象都必须可 JSON 序列化，
事件载荷经过 :func:`jsonable` 收敛（未知对象转字符串），避免一个宿主自定义
值把整条事件流打断。
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

PROTOCOL_VERSION = 1
JSONRPC_VERSION = "2.0"

#: 标准 JSON-RPC 错误码 + 本协议的会话错误码。
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
NOT_INITIALIZED = -32002
ALREADY_INITIALIZED = -32004
RUN_BUSY = -32001
SHUTTING_DOWN = -32003


class RpcError(Exception):
    """可安全回给宿主的协议错误；``data`` 不得放密钥/内部对象。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            body["data"] = jsonable(self.data)
        return body


def jsonable(value: Any) -> Any:
    """把事件载荷收敛成可 JSON 序列化的结构；未知对象退回字符串。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return str(value)


def encode(message: dict[str, Any]) -> bytes:
    """编码为一行 UTF-8 NDJSON（不含末尾换行）。"""
    return json.dumps(
        message, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")


def response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": jsonable(result)}


def error(request_id: Any, exc: RpcError) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": exc.to_dict(),
    }


def notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        message["params"] = jsonable(params)
    return message


def request(request_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "method": method,
        "params": jsonable(params),
    }
