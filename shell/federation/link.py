"""父核侧的本机 IPC 客户端（Phase 2）：读发现文件 → 握手 → 复用长连接调用子核工具。

与 `ChildCore`（进程内）同形：`manifest()` / `call()` / `node_id` / `child_id` / `product`，
因此 `Federation.register_child()` 不需要区分"进程内子核"还是"IPC 子核"。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from shell.federation.grants import (
    ChildCallFailed,
    ChildUnavailable,
    CoreGrant,
    GrantDenied,
)
from shell.federation.server import MAX_LINE_BYTES, PROTOCOL_VERSION, DiscoveryInfo


class LinkChild:
    """一条指向"另一个进程里的子核"的长连接。"""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        discovery: DiscoveryInfo,
        *,
        request_timeout: float,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.discovery = discovery
        self.request_timeout = request_timeout
        self.node_id = discovery.node_id
        self.child_id = discovery.child_id
        self.product = discovery.node_id
        self._manifest: dict[str, dict[str, Any]] = {}
        self._manifest_version = ""
        self._grant_token = ""
        self._next_id = 0
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False

    # ---------- 连接与握手 ----------

    @classmethod
    async def connect(
        cls,
        discovery_path: str | Path,
        *,
        parent_id: str,
        tools: Iterable[str] | None = None,
        subject: Any = None,
        parent_token: str | None = None,
        request_timeout: float = 30.0,
    ) -> LinkChild:
        path = Path(discovery_path)
        try:
            info = DiscoveryInfo.read(path)
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ChildUnavailable(f"读不到子核发现文件 {path}：{exc}") from exc
        if info.protocol != PROTOCOL_VERSION:
            raise ChildUnavailable(
                f"子核协议版本 {info.protocol} 与客户端 {PROTOCOL_VERSION} 不匹配"
            )
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(info.host, info.port, limit=MAX_LINE_BYTES),
                timeout=request_timeout,
            )
        except (OSError, TimeoutError) as exc:
            raise ChildUnavailable(f"连不上子核 {info.node_id}@{info.host}:{info.port}") from exc
        link = cls(reader, writer, info, request_timeout=request_timeout)
        link._reader_task = asyncio.create_task(link._read_loop())
        try:
            result = await link._request(
                "federation.handshake",
                {
                    "protocol": PROTOCOL_VERSION,
                    "parent_id": parent_id,
                    "token": info.token,
                    "tools": list(tools) if tools is not None else None,
                    "subject": subject,
                    "parent_token": parent_token,
                },
            )
        except Exception:
            await link.close()
            raise
        link._grant_token = str(result["grant"])
        link._manifest = {
            str(item["name"]): {
                "description": str(item["description"]),
                "input_schema": item["input_schema"],
            }
            for item in result["manifest"]
        }
        link.product = str(result.get("product") or info.node_id)
        link._manifest_version = str(result.get("manifest_version") or "")
        return link

    # ---------- 与 ChildCore 同形的能力面 ----------

    def manifest(self) -> dict[str, dict[str, Any]]:
        return dict(self._manifest)

    def manifest_version(self) -> str:
        return self._manifest_version

    def has_tool(self, name: str) -> bool:
        return name in self._manifest

    async def refresh_manifest(self) -> bool:
        """向子核拉一次当前可用工具；能力有变化时返回 True（供父核收敛）。"""
        result = await self._request(
            "federation.tools.list", {"grant": self._grant_token}
        )
        new_manifest = {
            str(item["name"]): {
                "description": str(item["description"]),
                "input_schema": item["input_schema"],
            }
            for item in result["tools"]
        }
        new_version = str(result.get("manifest_version") or "")
        changed = new_version != self._manifest_version or new_manifest != self._manifest
        self._manifest = new_manifest
        self._manifest_version = new_version
        return changed

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        grant: CoreGrant,
        subject: Any = None,
        call_id: str = "",
        chain: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        grant.check(self.child_id, tool)
        return await self._request(
            "federation.tools.call",
            {
                "grant": self._grant_token,
                "tool": tool,
                "arguments": dict(arguments or {}),
                "subject": subject,
                "call_id": call_id,
                "chain": list(chain),
            },
        )

    async def health(self) -> dict[str, Any]:
        return await self._request(
            "federation.health",
            {"token": self.discovery.token},
        )

    async def close(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ChildUnavailable("链路已关闭"))
        self._pending.clear()

    # ---------- 协议 ----------

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        if self._closed:
            raise ChildUnavailable("链路已关闭")
        self._next_id += 1
        request_id = str(self._next_id)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            self._writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await asyncio.wait_for(self._writer.drain(), timeout=self.request_timeout)
            return await asyncio.wait_for(future, timeout=self.request_timeout)
        except TimeoutError as exc:
            raise ChildCallFailed(f"子核 {self.node_id!r} 请求超时：{method}") from exc
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise ChildUnavailable(f"子核 {self.node_id!r} 连接已断开") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                future = self._pending.pop(str(message.get("id")), None)
                if future is None or future.done():
                    continue
                if "error" in message:
                    future.set_exception(_map_error(message["error"]))
                else:
                    future.set_result(message.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 读循环退出时把等待中的请求全部失败关闭
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ChildUnavailable("子核连接已关闭"))
            self._pending.clear()
            if not self._closed:
                self._closed = True


def _map_error(error: dict[str, Any]) -> Exception:
    message = str(error.get("message") or "unknown")
    code = error.get("code")
    if code == -32001 or message.startswith("grant_invalid"):
        return GrantDenied(message)
    return ChildCallFailed(message)
