"""生产级代码工具沙箱：一次性 Docker 容器实现 ``ToolSandbox``（宿主侧，可选）。

在教学级 :class:`SubprocessSandbox` 之上，把隔离边界从"进程"升级为"容器"，并保持
同一 ``ToolSandbox`` 协议——内核与 Agent 主循环零改动。容器提供 OS 级硬隔离：

- ``--rm``                  一次性，容器退出即删除
- ``--network none``        无网络命名空间（OS 级断网，不依赖白名单）
- ``--read-only``           根文件系统只读（另给一个小的 noexec tmpfs /tmp）
- ``--cap-drop ALL``        丢弃所有 Linux capabilities
- ``--security-opt no-new-privileges``  禁止提权
- ``--memory / --cpus``     内存 / CPU cgroup 上限（跨平台生效，补齐 Windows 无 rlimit）
- ``--pids-limit``          限制进程数，防 fork 炸弹

容器内入口复用 ``_sandbox_worker`` 的白名单 worker（纵深防御第二层）：其源码（剥掉
模块 docstring，以 ``from __future__`` 开头）经 ``python -c`` 传入，作为**单个 argv**
不经 shell、无转义问题；payload（code/inputs）走 stdin、envelope 走 stdout，不挂载
代码卷、不留临时文件。

依赖与边界：需宿主机 Docker 可达；**不进内核 dependencies**，属宿主侧能力。首次运行
需先 ``docker pull python:3.12-slim``。Docker 不可用时 :meth:`execute` 返回可读错误、
不抛穿到内核。更强隔离可换 gVisor / Firecracker / 远程 e2b，协议相同。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from pathlib import Path

from yai_core import SandboxResult

#: 复用子进程 worker 源码；剥到 from __future__ 起始，使其可作为 `python -c` 入口。
_WORKER_PATH = Path(__file__).resolve().parent / "_sandbox_worker.py"
_WORKER_SOURCE = _WORKER_PATH.read_text(encoding="utf-8")
_ENTRY_SOURCE = _WORKER_SOURCE[_WORKER_SOURCE.index("from __future__") :]


class DockerSandbox:
    """在一次性、强隔离 Docker 容器里执行代码工具的生产级沙箱。"""

    def __init__(
        self,
        *,
        # 默认跟随官方 3.12-slim 补丁 tag（docker run 动态拉取，能持续拿到安全更新）。
        # 供应链加固 / 完全可复现部署时，应把它 pin 到多架构 manifest list 摘要：
        #   image="python:3.12-slim@sha256:<digest>"
        # digest 用 `docker manifest inspect python:3.12-slim` 取最外层
        # Docker-Content-Digest（跨 amd64/arm64），并定期人工更新。构建期镜像
        # 已在仓库 Dockerfile 中 pin 摘要。
        image: str = "python:3.12-slim",
        timeout: float = 10.0,
        memory: str = "256m",
        cpus: float = 1.0,
        pids_limit: int = 64,
        docker_bin: str = "docker",
    ) -> None:
        self._image = image
        self._timeout = timeout
        self._memory = memory
        self._cpus = cpus
        self._pids = pids_limit
        self._docker = docker_bin

    def _build_argv(self, name: str) -> list[str]:
        """构造 docker run 的完整 argv（含全部隔离开关）；抽成方法便于离线核对。"""
        return [
            self._docker,
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            self._memory,
            "--cpus",
            str(self._cpus),
            "--pids-limit",
            str(self._pids),
            self._image,
            "python",
            "-I",
            "-S",
            "-X",
            "utf8",
            "-c",
            _ENTRY_SOURCE,
        ]

    async def execute(
        self,
        code: str,
        inputs: dict | None,
        *,
        timeout: float | None = None,
    ) -> SandboxResult:
        effective = float(timeout) if timeout is not None else self._timeout
        payload = json.dumps(
            {"code": code, "inputs": inputs or {}},
            ensure_ascii=False,
            default=str,
        )
        name = f"yai-sandbox-{uuid.uuid4().hex[:12]}"
        argv = self._build_argv(name)
        started = time.perf_counter()

        async def _force_remove() -> None:
            # docker run 客户端被杀不代表容器已停：按唯一名称 best-effort 强制删除。
            with contextlib.suppress(Exception):
                remover = await asyncio.create_subprocess_exec(
                    self._docker,
                    "rm",
                    "-f",
                    name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(remover.wait(), timeout=5)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return SandboxResult(
                ok=False,
                error="Docker 不可用：未找到 docker 命令，请先安装并启动 Docker",
                meta={"elapsed_ms": 0},
            )

        def _elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        async def _reap_client() -> None:
            # kill 后再 communicate 一次：排空并关闭客户端管道 transport，
            # 避免 Windows Proactor 事件循环报 "unclosed transport"。
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.communicate(), timeout=5)

        async def _communicate() -> tuple[bytes, bytes]:
            assert proc.stdin is not None
            return await proc.communicate(input=payload.encode("utf-8"))

        try:
            stdout_b, stderr_b = await asyncio.wait_for(_communicate(), timeout=effective)
        except TimeoutError:
            proc.kill()
            await _force_remove()
            await _reap_client()
            return SandboxResult(
                ok=False,
                error=f"容器执行超时（超过 {effective:g}s，容器已强制终止）",
                meta={"elapsed_ms": _elapsed_ms()},
            )
        except asyncio.CancelledError:
            # 外层取消：杀 docker run 客户端并强制删容器，再把取消向上传播。
            proc.kill()
            await _force_remove()
            await _reap_client()
            raise

        elapsed_ms = _elapsed_ms()
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace").strip()[-500:]
        lines = stdout.strip().splitlines()
        if not lines:
            hint = "（检查 Docker 是否启动、镜像是否已 pull）" if stderr else ""
            return SandboxResult(
                ok=False,
                error=f"容器无输出（exit={proc.returncode}）{hint}：{stderr}",
                meta={"elapsed_ms": elapsed_ms},
            )

        try:
            envelope = json.loads(lines[-1])
        except json.JSONDecodeError:
            return SandboxResult(
                ok=False,
                error=f"容器输出无法解析：{lines[-1][:300]}",
                meta={"elapsed_ms": elapsed_ms},
            )

        meta: dict[str, object] = {"elapsed_ms": elapsed_ms}
        if envelope.get("stdout"):
            meta["stdout"] = envelope["stdout"]
        if envelope.get("ok"):
            return SandboxResult(ok=True, output=envelope.get("output"), meta=meta)
        return SandboxResult(ok=False, error=envelope.get("error", "未知沙箱错误"), meta=meta)
