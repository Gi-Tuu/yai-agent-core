"""host_g 演示：一个最小可运行的 ``ToolSandbox`` 实现（子进程隔离）。

它实现 ``src/yai_core/spi/sandbox.py`` 定义的 ``ToolSandbox`` 协议：内核把
模型生成的代码工具交给这里，在一次性子进程的受限环境（见 ``_worker.py``）
里执行，再拿回结构化结果。内核本体始终零硬依赖、不内置任何执行器。

隔离手段（教学级，非容器）：
- 独立子进程，崩溃 / 死循环不影响内核进程，超时直接杀死（跨平台，主手段）；
- 以 ``python -I -S -X utf8`` 启动 worker：忽略 PYTHONPATH、环境变量、
  user site 与 site-packages，并强制 UTF-8；
- worker 内置白名单，模型代码无法 import（拿不到 socket/os）、无法 open；
- 每次调用都有超时（默认 10s）；
- worker 对 print 输出（4k 字符）与最终结果（64KB）设上界，超量丢弃 / 判失败；
- Unix（Linux/macOS）额外用 ``resource`` 限制内存（256MB）与 CPU（15s）。

**它防什么 / 不防什么（诚实边界）**：
- 防：模型代码崩溃、死循环、误删文件、普通导入与联网、无限打印、返回超大结果；
- 不防：蓄意的 Python 沙箱逃逸（白名单不是安全边界）；
- 平台差异：**Windows 没有标准库等价的内存 / CPU rlimit，内存不硬限**，
  只靠超时杀进程；Unix 才有内存与 CPU 硬限；
- 不做操作系统级网络隔离（无网络命名空间），"不能联网"仅来自白名单缺 socket。

结论：它适合跑"模型生成、且已过权限确认"的低风险代码工具。生产环境要跑
不可信代码时，应换成容器（Docker）/ gVisor / 微 VM（Firecracker）/ 远程
沙箱（e2b）实现同一协议，内核与 Agent 主循环无需改动（见 ROADMAP）。
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
