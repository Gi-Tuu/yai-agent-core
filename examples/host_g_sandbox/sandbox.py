"""host_g 演示：一个最小可运行的 ``ToolSandbox`` 实现（子进程隔离）。

它实现 ``src/yai_core/spi/sandbox.py`` 定义的 ``ToolSandbox`` 协议：内核把
模型生成的代码工具交给这里，在一次性子进程的受限环境（见 ``_worker.py``）
里执行，再拿回结构化结果。内核本体始终零硬依赖、不内置任何执行器。

隔离手段（教学级，非容器）：
- 独立子进程，崩溃 / 死循环不影响内核进程，超时直接杀死；
- 以 ``python -I -S -X utf8`` 启动 worker：忽略 PYTHONPATH、环境变量、
  user site 与 site-packages，并强制 UTF-8；
- worker 内置白名单，模型代码无法 import（拿不到 socket/os）、无法 open；
- 每次调用都有超时（默认 10s）。

生产环境要跑不可信代码时，应换成容器 / gVisor / 微 VM 实现同一协议，
内核与 Agent 主循环无需改动。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from yai_core import SandboxResult

_WORKER = Path(__file__).resolve().parent / "_worker.py"


class SubprocessSandbox:
    """在受限子进程里执行代码工具的最小沙箱。"""

    def __init__(self, *, timeout: float = 10.0, python: str | None = None) -> None:
        self._timeout = timeout
        self._python = python or sys.executable

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
        # -I 隔离模式（忽略环境变量/PYTHONPATH/user site）；-S 不加载 site；
        # -X utf8 强制 UTF-8，避免 Windows 默认代码页导致中文乱码。
        argv = [self._python, "-I", "-S", "-X", "utf8", str(_WORKER)]
        loop = asyncio.get_running_loop()
        started = time.perf_counter()

        def _run() -> subprocess.CompletedProcess:
            return subprocess.run(
                argv,
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=effective,
                check=False,
            )

        try:
            proc = await loop.run_in_executor(None, _run)
        except subprocess.TimeoutExpired:
            return SandboxResult(
                ok=False,
                error=f"代码执行超时（超过 {effective:g}s，子进程已终止）",
                meta={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
            )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        lines = (proc.stdout or "").strip().splitlines()
        if not lines:
            stderr = (proc.stderr or "").strip()[-500:]
            return SandboxResult(
                ok=False,
                error=f"沙箱无输出（exit={proc.returncode}）：{stderr}",
                meta={"elapsed_ms": elapsed_ms},
            )

        try:
            envelope = json.loads(lines[-1])
        except json.JSONDecodeError:
            return SandboxResult(
                ok=False,
                error=f"沙箱输出无法解析：{lines[-1][:300]}",
                meta={"elapsed_ms": elapsed_ms},
            )

        meta: dict[str, object] = {"elapsed_ms": elapsed_ms}
        if envelope.get("stdout"):
            meta["stdout"] = envelope["stdout"]
        if envelope.get("ok"):
            return SandboxResult(ok=True, output=envelope.get("output"), meta=meta)
        return SandboxResult(
            ok=False,
            error=envelope.get("error", "未知沙箱错误"),
            meta=meta,
        )
