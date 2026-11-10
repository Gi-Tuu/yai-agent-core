"""常驻子核的生命周期托管（Phase 3）：拉起子进程 → 等发现文件 → 连接 → 重连 / 停止。

只做宿主层编排：不引入服务管理器、不守护系统级进程；进程的生命周期由宿主决定。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from shell.federation.grants import ChildUnavailable
from shell.federation.link import LinkChild


class ManagedChild:
    """把一个"子产品进程"托管起来，并给父核一个可用的 `LinkChild`。

    Args:
        command: 启动子进程的命令（如 ``[sys.executable, "child_app.py", ...]``）。
        discovery_dir: 发现文件目录（必须与子进程参数一致）。
        node_id: 子核在发现文件里的节点名。
        parent_id: 父核身份（写进 grant 的 edge）。
        tools: 握手时申请的工具子集；None = 全部（IPC 下为通配 grant）。
        connect_timeout: 等发现文件 + 握手的墙钟上限。
        env: 追加的环境变量（默认继承当前环境并强制 UTF-8 输出）。
    """

    def __init__(
        self,
        command: Iterable[str],
        *,
        discovery_dir: str | Path,
        node_id: str,
        parent_id: str = "root",
        tools: Iterable[str] | None = None,
        connect_timeout: float = 20.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = [str(part) for part in command]
        if not self.command:
            raise ValueError("command 不能为空")
        self.discovery_dir = Path(discovery_dir)
        self.node_id = node_id
        self.parent_id = parent_id
        self.tools = list(tools) if tools is not None else None
        self.connect_timeout = float(connect_timeout)
        self.env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            **(env or {}),
        }
        self.process: subprocess.Popen | None = None
        self.link: LinkChild | None = None
        self.discovery_path = self.discovery_dir / f"{node_id}.json"

    # ---------- 生命周期 ----------

    async def start(self) -> LinkChild:
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("ManagedChild 已在运行")
        self.discovery_dir.mkdir(parents=True, exist_ok=True)
        self.discovery_path.unlink(missing_ok=True)
        self.process = subprocess.Popen(  # noqa: S603 - 命令由宿主显式提供
            self.command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self.env,
        )
        return await self._connect_when_ready()

    async def stop(self) -> None:
        if self.link is not None:
            await self.link.close()
            self.link = None
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 10)
        self.discovery_path.unlink(missing_ok=True)

    async def restart(self) -> LinkChild:
        await self.stop()
        return await self.start()

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    async def ensure(self) -> LinkChild:
        """返回可用连接；连接已断但进程还活着时自动重连，进程死了则失败关闭。"""
        if self.link is not None and not getattr(self.link, "_closed", False) and self.alive():
            return self.link
        if not self.alive():
            raise ChildUnavailable(f"子核进程 {self.node_id!r} 已退出")
        return await self._connect_when_ready()

    async def _connect_when_ready(self) -> LinkChild:
        deadline = time.monotonic() + self.connect_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if not self.alive():
                raise ChildUnavailable(
                    f"子核进程已退出（exit={self.process.returncode if self.process else '?'}）："
                    f"{' '.join(self.command)}"
                )
            if self.discovery_path.is_file():
                try:
                    self.link = await LinkChild.connect(
                        self.discovery_path,
                        parent_id=self.parent_id,
                        tools=self.tools,
                    )
                    return self.link
                except Exception as exc:  # noqa: BLE001 - 启动期握手失败可重试
                    last_error = exc
            await asyncio.sleep(0.05)
        raise ChildUnavailable(f"等不到子核 {self.node_id!r} 就绪：{last_error or 'timeout'}")

    # ---------- 便捷调用 ----------

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        grant: Any = None,
        subject: Any = None,
    ):
        link = await self.ensure()
        if grant is None:
            from shell.federation.grants import CoreGrant

            grant = CoreGrant(
                edge=f"{self.parent_id}->{self.node_id}",
                child_id=link.child_id,
                tools=frozenset(link.manifest()),
            )
        return await link.call(tool, arguments or {}, grant=grant, subject=subject)

    async def __aenter__(self) -> ManagedChild:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()
