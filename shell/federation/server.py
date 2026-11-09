"""子核侧的本机 IPC 服务（Phase 2）：loopback TCP + NDJSON JSON-RPC + 发现文件 + HMAC grant。

设计见 `docs/guides/federation.md` §5。安全边界（照实写）：

- 只绑定 ``127.0.0.1``；发现文件权限 0600、含一次性连接 token；
- 握手成功后由服务端**签发 HMAC 签名的 grant**（密钥只在子核进程内，不写发现文件），
  每次调用都要校验 grant 的签名 / 过期 / 工具子集；
- 前提是同 OS 用户：**不防 root、调试器、内存抓取**，也不替代子核自己的策略。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shell.federation.grants import CoreGrant, GrantDenied
from shell.federation.node import ChildCore
from shell.federation.parent import _CALL_CHAIN

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 1 << 20  # 单条消息上限 1 MiB
DEFAULT_GRANT_TTL = 3600.0


class FederationProtocolError(RuntimeError):
    """协议层错误（版本不匹配 / 帧过大 / 非法 JSON）。"""


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sign(secret: bytes, payload: dict[str, Any]) -> str:
    return hmac.new(secret, _canonical(payload), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class DiscoveryInfo:
    """子核的发现文件内容（同用户可读；token 是一次性连接凭据）。"""

    node_id: str
    child_id: str
    host: str
    port: int
    token: str
    pid: int
    protocol: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "node_id": self.node_id,
            "child_id": self.child_id,
            "host": self.host,
            "port": self.port,
            "token": self.token,
            "pid": self.pid,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiscoveryInfo:
        return cls(
            node_id=str(data["node_id"]),
            child_id=str(data["child_id"]),
            host=str(data["host"]),
            port=int(data["port"]),
            token=str(data["token"]),
            pid=int(data.get("pid", 0)),
            protocol=int(data.get("protocol", 0)),
        )

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.node_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")
        with os.chmod(tmp, 0o600) if os.name == "posix" else _noop_chmod(tmp):
            pass
        os.replace(tmp, path)
        return path

    @classmethod
    def read(cls, path: Path) -> DiscoveryInfo:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


class _noop_chmod:
    """Windows 上 chmod 语义受限：显式空操作，避免误以为设了 0600。"""

    def __init__(self, _path: Path) -> None:
        pass

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None


class ChildServer:
    """把 `ChildCore` 暴露成本机 JSON-RPC 服务。

    Args:
        child: 被暴露的子核。
        discovery_dir: 发现文件目录（默认 ``<data>/yai-federation``）。
        parent_tools: 可选的"父核 → 工具子集"白名单；None 表示任何能出示 token 的父核。
        grant_ttl: grant 有效期（秒）。
    """

    def __init__(
        self,
        child: ChildCore,
        *,
        discovery_dir: Path | None = None,
        parent_tools: dict[str, Iterable[str]] | None = None,
        grant_ttl: float = DEFAULT_GRANT_TTL,
    ) -> None:
        if grant_ttl <= 0:
            raise ValueError("grant_ttl 必须为正数")
        self.child = child
        self.discovery_dir = discovery_dir or _default_discovery_dir()
        self._parent_tools = (
            None
            if parent_tools is None
            else {key: frozenset(value) for key, value in parent_tools.items()}
        )
        self.grant_ttl = float(grant_ttl)
        self._secret = secrets.token_bytes(32)
        self._token = secrets.token_urlsafe(32)
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.StreamWriter] = set()
        self.discovery: DiscoveryInfo | None = None
        self.discovery_path: Path | None = None

    # ---------- 生命周期 ----------

    async def start(self, *, host: str = "127.0.0.1", port: int = 0) -> DiscoveryInfo:
        if self._server is not None:
            raise RuntimeError("ChildServer 已启动")
        self._server = await asyncio.start_server(self._handle, host, port, limit=MAX_LINE_BYTES)
        sock = self._server.sockets[0]
        bound_host, bound_port = sock.getsockname()[:2]
        self.discovery = DiscoveryInfo(
            node_id=self.child.node_id,
            child_id=self.child.child_id,
            host=str(bound_host),
            port=int(bound_port),
            token=self._token,
            pid=os.getpid(),
        )
        self.discovery_path = self.discovery.write(self.discovery_dir)
        return self.discovery

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for writer in list(self._connections):
            writer.close()
            with _suppress():
                await writer.wait_closed()
        self._connections.clear()
        if self.discovery_path is not None:
            with _suppress():
                self.discovery_path.unlink()
            self.discovery_path = None

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    # ---------- 协议 ----------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._connections.add(writer)
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    await _send(writer, _error(None, -32700, "frame too large"))
                    return
                if not line:
                    return
                try:
                    request = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await _send(writer, _error(None, -32700, "invalid json"))
                    continue
                response = await self._dispatch(request)
                await _send(writer, response)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._connections.discard(writer)
            writer.close()
            with _suppress():
                await writer.wait_closed()

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, -32602, "params 必须是对象")
        try:
            if method == "federation.health":
                return _ok(request_id, await self._health(params))
            if method == "federation.handshake":
                return _ok(request_id, await self._handshake(params))
            if method == "federation.tools.list":
                return _ok(request_id, self._tools_list(params))
            if method == "federation.tools.call":
                return _ok(request_id, await self._tools_call(params))
            return _error(request_id, -32601, f"未知方法：{method!r}")
        except GrantDenied as exc:
            return _error(request_id, -32001, f"grant_invalid: {exc}")
        except FederationProtocolError as exc:
            return _error(request_id, -32002, f"protocol_error: {exc}")
        except Exception as exc:  # noqa: BLE001 - 协议层兜底，不让异常打断连接
            return _error(request_id, -32603, f"internal_error: {type(exc).__name__}: {exc}")

    # ---------- 方法 ----------

    async def _health(self, params: dict[str, Any]) -> dict[str, Any]:
        self._check_token(params)
        return {
            "ok": True,
            "node_id": self.child.node_id,
            "child_id": self.child.child_id,
            "protocol": PROTOCOL_VERSION,
            "tools": len(self.child.manifest()),
        }

    async def _handshake(self, params: dict[str, Any]) -> dict[str, Any]:
        protocol = params.get("protocol")
        if protocol != PROTOCOL_VERSION:
            raise FederationProtocolError(
                f"协议版本不匹配：server={PROTOCOL_VERSION} client={protocol!r}"
            )
        self._check_token(params)
        parent_id = str(params.get("parent_id") or "").strip()
        if not parent_id:
            raise FederationProtocolError("handshake 缺少 parent_id")
        manifest = self.child.manifest()
        if self._parent_tools is None:
            granted = set(manifest)
        else:
            allowed = self._parent_tools.get(parent_id)
            if allowed is None:
                raise GrantDenied(f"父核 {parent_id!r} 不在允许列表内")
            granted = set(allowed)
        requested = params.get("tools")
        if requested is not None:
            if not isinstance(requested, list) or not all(isinstance(x, str) for x in requested):
                raise FederationProtocolError("tools 必须是字符串数组")
            granted &= set(requested)
        unknown = granted - set(manifest)
        if unknown:
            raise FederationProtocolError(f"这些工具不存在：{sorted(unknown)}")
        if not granted:
            raise GrantDenied("父核请求的工具子集为空")
        expires_at = time.time() + self.grant_ttl
        payload = {
            "v": 1,
            "edge": f"{parent_id}->{self.child.node_id}",
            "child_id": self.child.child_id,
            "tools": sorted(granted),
            "exp": expires_at,
        }
        grant = {"payload": payload, "sig": _sign(self._secret, payload)}
        return {
            "protocol": PROTOCOL_VERSION,
            "node_id": self.child.node_id,
            "child_id": self.child.child_id,
            "product": self.child.product,
            "manifest": [
                {
                    "name": name,
                    "description": meta["description"],
                    "input_schema": meta["input_schema"],
                }
                for name, meta in manifest.items()
            ],
            "grant": json.dumps(grant, ensure_ascii=False),
        }

    def _tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = self._verify_grant(params.get("grant"))
        return {
            "tools": [
                {"name": name, "description": manifest["description"]}
                for name, manifest in self.child.manifest().items()
                if name in payload["tools"]
            ]
        }

    async def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = self._verify_grant(params.get("grant"))
        tool = str(params.get("tool") or "")
        if tool not in payload["tools"]:
            raise GrantDenied(f"工具 {tool!r} 不在 grant 子集内")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise FederationProtocolError("arguments 必须是对象")
        subject = params.get("subject", payload.get("subject"))
        call_id = str(params.get("call_id") or "")
        deadline_ms = params.get("deadline_ms")
        chain = tuple(params.get("chain") or ())
        grant = CoreGrant(
            edge=str(payload["edge"]),
            child_id=str(payload["child_id"]),
            tools=frozenset(payload["tools"]),
            subject=subject,
        )
        run = self._run_tool(
            tool,
            arguments,
            grant=grant,
            subject=subject,
            call_id=call_id,
            chain=chain,
        )
        if deadline_ms is None:
            return await run
        try:
            async with asyncio.timeout(float(deadline_ms) / 1000.0):
                return await run
        except TimeoutError as exc:
            raise FederationProtocolError("deadline_exceeded") from exc

    async def _run_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        grant: CoreGrant,
        subject: Any,
        call_id: str,
        chain: tuple[str, ...],
    ) -> dict[str, Any]:
        token = _CALL_CHAIN.set(chain)
        try:
            return await self.child.call(
                tool, arguments, grant=grant, subject=subject, call_id=call_id
            )
        finally:
            _CALL_CHAIN.reset(token)

    # ---------- 鉴权 ----------

    def _check_token(self, params: dict[str, Any]) -> None:
        token = str(params.get("token") or "")
        if not hmac.compare_digest(token, self._token):
            raise GrantDenied("连接 token 无效")

    def _verify_grant(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, str):
            raise GrantDenied("缺少 grant")
        try:
            grant = json.loads(raw)
            payload = grant["payload"]
            sig = grant["sig"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise GrantDenied("grant 格式非法") from exc
        if not hmac.compare_digest(str(sig), _sign(self._secret, payload)):
            raise GrantDenied("grant 签名无效")
        if float(payload.get("exp", 0)) <= time.time():
            raise GrantDenied("grant 已过期")
        if payload.get("child_id") != self.child.child_id:
            raise GrantDenied("grant 不属于本子核")
        return payload


def _ok(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    await writer.drain()


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return True


def _default_discovery_dir() -> Path:
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_RUNTIME_DIR") or str(Path.home())
    return Path(base) / "yai-federation"
