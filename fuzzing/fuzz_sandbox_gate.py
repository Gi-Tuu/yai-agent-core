"""Fuzz target：代码工具沙箱的 **AST 静态闸门**（宿主参考实现 + 桌面产品实现）。

被测函数是 exec 之前那道 `_static_check(code)`：模型生成的代码完全由模型（以及
上游 prompt injection）控制，这道闸是它进入沙箱前的第一层拦截，所以值得持续模糊。
两份实现逐字相同，本 target 同时喂两份并比对——**两份漂移本身就是 bug**。

不变量：

1. **不放行违规代码**：独立 oracle（`ast.walk` 全量扫描，迭代式、不受递归深度限制）
   认定树里有 `import` / `from ... import` / 下划线开头的属性访问时，闸门必须以
   `_SafetyError` 拒绝；
2. **只抛自己的异常**：闸门允许抛 `_SafetyError`（以及解析阶段的 `SyntaxError` /
   `ValueError`），其它异常一律算 finding。这条不是纸面担忧——实测 600 段属性链
   `x.b.b.b…` 能被 PEG 解析器接受，却把 `NodeVisitor` 的递归撑爆，
   `RecursionError` 原本会穿透闸门；现在它在闸门内转成拒绝（宁可误拒不可误放）；
3. **普通干净代码不被误拒**：节点数很少且 oracle 判干净的输入必须放行，
   这样"深度拒绝"不会被当成万能挡箭牌；
4. **确定性**：同一输入两次调用结论一致，两份实现结论一致。

平台/运行方式同 :mod:`fuzz_features`（atheris 只在 Linux x86_64 有 wheel）：

    uv run python fuzzing/fuzz_sandbox_gate.py -max_total_time=45
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import atheris

_ROOT = Path(__file__).resolve().parent.parent

#: 节点数不超过这个量的干净代码必须放行（深嵌套拒绝只允许发生在真深的树上）。
_SMALL_TREE_NODES = 100

#: 语料前缀：帮 fuzzer 从"能解析的 Python"起步，而不是从随机字节里撞语法。
_SEEDS = (
    "",
    "x = 1\n",
    "import os\n",
    "from os import path\n",
    "def run(inputs):\n    return ",
    "print(",
    "x.__class__",
    "x.b",
    "[i for i in range(",
    "lambda f: f.__globals__",
    "try:\n    1/0\nexcept Exception as e:\n    e.__traceback__\n",
)


def _load(name: str, rel: str) -> ModuleType:
    """按文件路径装载 worker 模块（两份都不是包，只能用 location 直接 load）。"""
    path = _ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 路径写错时立刻炸
        raise RuntimeError(f"无法装载 {rel}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


WORKERS = (
    ("fuzz_gate_worker_example", "examples/host_g_sandbox/_worker.py"),
    ("fuzz_gate_worker_desktop", "shell/desktop/_sandbox_worker.py"),
)
_MODULES = tuple(_load(n, p) for n, p in WORKERS)


def _forbidden(tree: ast.AST) -> bool:
    """独立 oracle：全量扫描 AST，不依赖被测量代码的遍历方式。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return True
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return True
    return False


def _verdict(module: ModuleType, code: str) -> str:
    """把闸门结论压成 'allowed' / 'blocked'；其它异常原样上抛（= finding）。"""
    try:
        module._static_check(code)
    except module._SafetyError:
        return "blocked"
    return "allowed"


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    seed = fdp.PickValueInArray(list(_SEEDS))
    tail = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 1200))
    code = f"{seed}{tail}"

    # 解析阶段一视同仁：两份实现用同一个 ast.parse，解析不了就不归闸门管。
    try:
        tree = ast.parse(code, filename="<fuzz>")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return

    bad = _forbidden(tree)
    small = sum(1 for _ in ast.walk(tree)) <= _SMALL_TREE_NODES

    verdicts: list[str] = []
    for module in _MODULES:
        verdict = _verdict(module, code)
        # 1) 违规代码绝不能放行
        if bad:
            assert verdict == "blocked", f"闸门放行了违规代码: {code!r}"
        # 3) 小而干净的代码不许被拒（深度拒绝只许发生在真深的树上）
        elif small:
            assert verdict == "allowed", f"误拒了简单的干净代码: {code!r}"
        # 4) 确定性
        assert _verdict(module, code) == verdict, f"同一输入两次结论不同: {code!r}"
        verdicts.append(verdict)

    # 4) 两份实现必须一致
    assert verdicts[0] == verdicts[1], f"两份闸门漂移: {code!r} → {verdicts}"


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
