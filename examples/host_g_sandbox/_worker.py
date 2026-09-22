"""代码工具沙箱的子进程执行器（host_g 演示用）。

由 ``sandbox.py`` 的 ``SubprocessSandbox`` 以
``python -I -S -X utf8 _worker.py`` 启动，通过标准输入收到一段 JSON：

    {"code": "<模型生成的 Python 代码>", "inputs": {...}}

然后在**受限内置环境**里执行代码中定义的 ``run(inputs)``，把结果以
一行 JSON 打到标准输出：

    {"ok": true,  "output": <可 JSON 序列化结果>, "stdout": "<被捕获的 print>"}
    {"ok": false, "error": "<类型: 信息>",        "stdout": "<...>"}

隔离手段（教学级，不是容器）：
- 运行在一次性独立子进程里，崩溃 / 死循环不影响内核，超时由父进程杀死；
- ``-I -S`` 启动：忽略 PYTHONPATH、环境变量、user site 与 site-packages；
- 内置白名单：没有 ``__import__`` / ``open`` / ``eval`` / ``exec`` /
  ``compile`` / ``getattr`` / ``__build_class__`` 等，模型代码无法导入
  os/socket、无法读写文件、无法定义类、无法做内省逃逸；
- 模型代码里的 ``print`` 被捕获到一个**有上界**的缓冲，超量丢弃，不撑爆内存；
- 最终结果 JSON 有字节上限，超限直接判失败；
- Unix 下额外用 ``resource`` 限制地址空间（内存）与 CPU 秒数。

**诚实的安全边界（务必读）**：
- 仅靠 Python 内置白名单**无法 100% 防住蓄意的沙箱逃逸**；
- 本实现**不做网络隔离**：白名单里没有 socket，常规代码无法联网，但这不是
  操作系统级的网络命名空间，不能视为网络安全边界；
- **Windows 没有标准库等价的内存 / CPU rlimit**：Windows 上内存不硬限，
  只靠父进程超时杀进程兜底；Unix（Linux/macOS）才有内存与 CPU 硬限；
- 因此它只适合跑"模型生成、权限已确认"的低风险代码。真正跑不可信代码的
  生产环境请改用容器 / gVisor / 微 VM（Firecracker）/ 远程沙箱（e2b）实现
  同一个 ``ToolSandbox`` 协议——内核与 Agent 主循环一行都不用改。
"""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import math
import sys
import traceback

# ---- 资源与输出上界 -------------------------------------------------------
#: 被捕获 print 的最大字符数：超过后直接丢弃，避免执行期无限 print 撑爆内存。
_MAX_STDOUT_CHARS = 4000
#: 最终结果（output）序列化后的最大字节数，超过判失败。
_MAX_OUTPUT_BYTES = 64 * 1024
#: 子进程最大地址空间（字节），仅 Unix 生效。
_MEM_LIMIT_BYTES = 256 * 1024 * 1024
#: 子进程累计 CPU 秒硬上限，仅 Unix 生效（父进程超时是跨平台主手段）。
_CPU_SECONDS = 15

# 只暴露纯计算与常用内置；刻意剔除一切能导入模块、读写文件、反射内省、
# 动态编译或自定义类的入口。True/False/None 是关键字，无需列入。
_ALLOWED_NAMES = (
    "abs", "all", "any", "ascii", "bin", "bool", "chr", "dict", "divmod",
    "enumerate", "filter", "float", "format", "frozenset", "hash", "hex",
    "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max",
    "min", "next", "oct", "ord", "pow", "print", "range", "repr", "reversed",
    "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip",
    # 常见异常类型，允许工具内做正常的判断/抛错。
    "Exception", "ArithmeticError", "StopIteration", "ValueError", "TypeError",
    "KeyError", "IndexError", "ZeroDivisionError",
)

_SAFE_BUILTINS: dict[str, object] = {
    name: getattr(builtins, name)
    for name in _ALLOWED_NAMES
    if hasattr(builtins, name)
}
# math 是纯计算标准库（无 IO），预导入后直接给，模型仍拿不到 import 能力。
_SAFE_BUILTINS["math"] = math
_SAFE_BUILTINS["__name__"] = "__yai_sandbox__"


class _CappedStream(io.StringIO):
    """有上界的 stdout 缓冲：写满后丢弃后续内容，避免内存无限增长。"""

    def __init__(self, cap: int = _MAX_STDOUT_CHARS) -> None:
        super().__init__()
        self._cap = cap
        self.truncated = False

    def write(self, s: str) -> int:  # type: ignore[override]
        if self.tell() >= self._cap:
            self.truncated = True
            return len(s)
        return super().write(s)


def _apply_resource_limits() -> None:
    """Unix 上限制子进程内存与 CPU；Windows 无标准库等价能力，直接跳过。"""

    if sys.platform.startswith("win"):
        return
    try:
        import resource
    except ImportError:  # pragma: no cover - 某些精简环境无 resource
        return
    if hasattr(resource, "RLIMIT_AS"):
        resource.setrlimit(
            resource.RLIMIT_AS, (_MEM_LIMIT_BYTES, _MEM_LIMIT_BYTES)
        )
    if hasattr(resource, "RLIMIT_CPU"):
        resource.setrlimit(resource.RLIMIT_CPU, (_CPU_SECONDS, _CPU_SECONDS))


def _emit(captured: _CappedStream, *, ok: bool, output=None, error=None) -> None:
    text = captured.getvalue()[:_MAX_STDOUT_CHARS]
    if captured.truncated and len(text) == _MAX_STDOUT_CHARS:
        text += "\n…[输出过长已截断]"
    envelope: dict[str, object] = {"ok": ok, "stdout": text}
    if ok:
        envelope["output"] = output
    else:
        envelope["error"] = error
    # 用 sys.__stdout__ 输出协议行，避免被 redirect_stdout 影响。
    sys.__stdout__.write(
        json.dumps(envelope, ensure_ascii=False, default=str) + "\n"
    )
    sys.__stdout__.flush()


def main() -> None:
    # 越早施加资源上限越好（必须在执行模型代码之前）。
    _apply_resource_limits()
    captured = _CappedStream()
    try:
        payload = json.loads(sys.stdin.read())
        code = payload["code"]
        inputs = payload.get("inputs") or {}
    except Exception as exc:  # noqa: BLE001 - 入参错误也要回 envelope
        _emit(captured, ok=False, error=f"入参解析失败: {type(exc).__name__}: {exc}")
        return

    # 关键：受限 globals。__builtins__ 换成白名单后，模型代码里的
    # import / open / 反射等都会因找不到内置而 NameError。
    safe_globals: dict[str, object] = {"__builtins__": dict(_SAFE_BUILTINS)}
    try:
        with contextlib.redirect_stdout(captured):
            # compile/exec 是 worker 自己（可信）在用，执行环境仍是受限 globals。
            exec(compile(code, "<code-tool>", "exec"), safe_globals)  # noqa: S102
            run = safe_globals.get("run")
            if not callable(run):
                raise ValueError("代码必须定义一个可调用函数 run(inputs: dict)")
            output = run(inputs)
        # 在 worker 侧先做一次可序列化校验（default=str 兜底）。
        encoded = json.dumps(output, ensure_ascii=False, default=str).encode("utf-8")
        if len(encoded) > _MAX_OUTPUT_BYTES:
            _emit(
                captured,
                ok=False,
                error=(
                    f"结果过大（{len(encoded)} 字节 > {_MAX_OUTPUT_BYTES} 上限），"
                    "请缩小返回内容（如只返回摘要 / 前若干条）"
                ),
            )
            return
        output = json.loads(encoded)
    except MemoryError:
        _emit(captured, ok=False, error="代码触发内存上限（MemoryError），已终止")
        return
    except Exception as exc:  # noqa: BLE001 - 任何失败都回灌，不穿透到父进程
        last = traceback.format_exception_only(type(exc), exc)
        _emit(captured, ok=False, error=last[-1].strip() if last else str(exc))
        return

    _emit(captured, ok=True, output=output)


if __name__ == "__main__":
    main()
