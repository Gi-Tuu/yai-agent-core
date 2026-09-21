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
- 模型代码里的 ``print`` 被捕获到 ``stdout`` 字段，不污染结果协议。

注意：仅靠 Python 内置白名单无法 100% 防住蓄意的沙箱逃逸；真正跑不可信
代码的生产环境请改用容器 / gVisor / 微 VM。这里的价值是演示"内核如何通过
ToolSandbox SPI 把代码执行安全地交还给宿主"。
"""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import math
import sys
import traceback

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


def _emit(captured: io.StringIO, *, ok: bool, output=None, error=None) -> None:
    envelope: dict[str, object] = {"ok": ok, "stdout": captured.getvalue()[:4000]}
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
    captured = io.StringIO()
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
        output = json.loads(json.dumps(output, ensure_ascii=False, default=str))
    except Exception as exc:  # noqa: BLE001 - 任何失败都回灌，不穿透到父进程
        last = traceback.format_exception_only(type(exc), exc)
        _emit(captured, ok=False, error=last[-1].strip() if last else str(exc))
        return

    _emit(captured, ok=True, output=output)


if __name__ == "__main__":
    main()
