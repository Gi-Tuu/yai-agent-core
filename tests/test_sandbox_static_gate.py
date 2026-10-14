"""沙箱 AST 静态闸门的回归（两份实现：宿主参考示例 + 桌面产品层）。

闸门是"模型生成的代码"进沙箱前的第一层拦截，输入完全由模型控制，
因此这里跑的是**畸形 / 对抗文本**下的三条承诺：

1. import 与下划线属性访问必须被拒（反射逃逸的公共前提）；
2. 只有 `_SafetyError` 能代表"拒绝"——别的异常穿透闸门就等于把
   "闸门坏了"伪装成"没意见"，深嵌套属性链正是这种输入（实测 600 段
   链能过 PEG 解析器，却把 ``NodeVisitor`` 递归撑爆）；
3. 普通干净代码不许误拒——否则"失败关闭"会变成万能挡箭牌。

离线、不起子进程：只装载两个 worker 模块，直接调它们的 `_static_check`。
持续模糊见 ``fuzzing/fuzz_sandbox_gate.py``（atheris 仅 Linux wheel）。
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

from host_g_sandbox import _worker as example_worker  # noqa: E402


def _load(name: str, rel: str) -> ModuleType:
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


desktop_worker = _load(
    "test_desktop_sandbox_worker", "shell/desktop/_sandbox_worker.py"
)
WORKERS = [example_worker, desktop_worker]
WORKER_IDS = ["examples", "desktop"]


@pytest.fixture(params=WORKERS, ids=WORKER_IDS)
def worker(request: pytest.FixtureRequest) -> ModuleType:
    return request.param


# ---------- 1. 该拒的必须拒 ----------

BLOCKED = [
    "import os\ndef run(inputs):\n    return 1",
    "from os import path\ndef run(inputs):\n    return 1",
    "def run(inputs):\n    import json\n    return json",
    "def run(inputs):\n    return inputs.__class__",
    "def run(inputs):\n    return (lambda f: f.__globals__)(run)",
    "def run(inputs):\n    return type(inputs).__mro__",
    "def run(inputs):\n    try:\n        1 / 0\n    except Exception as e:\n"
    "        return e.__traceback__",
    "def run(inputs):\n    return inputs.b.__dict__",
]


@pytest.mark.parametrize("code", BLOCKED, ids=[str(i) for i in range(len(BLOCKED))])
def test_reflection_and_import_are_blocked(worker: ModuleType, code: str) -> None:
    with pytest.raises(worker._SafetyError):
        worker._static_check(code)


def test_deep_attribute_chain_fails_closed(worker: ModuleType) -> None:
    """600 段属性链能解析、但会炸掉 NodeVisitor 递归 → 必须转成拒绝。

    漏出 ``RecursionError`` 的闸门是坏掉的闸门：只捕 `_SafetyError` 的调用方
    会把它当内部错误，而不是当"这段代码不许跑"。
    """
    code = "def run(inputs):\n    return inputs" + ".b" * 600
    ast.parse(code)  # 前置条件：解析器接受它，拒绝只能来自闸门
    with pytest.raises(worker._SafetyError):
        worker._static_check(code)


# ---------- 2. 不该拒的别拒 ----------

ALLOWED = [
    "def run(inputs):\n    return inputs['a'] + 1",
    "def run(inputs):\n    return sorted(inputs, key=str)",
    "def run(inputs):\n    total = 0\n    for k, v in inputs.items():\n"
    "        total += v\n    return total",
    "import_free = 1\ndef run(inputs):\n    return import_free",
    "def run(inputs):\n    return math.sqrt(2)",
]


@pytest.mark.parametrize("code", ALLOWED, ids=[str(i) for i in range(len(ALLOWED))])
def test_clean_code_is_allowed(worker: ModuleType, code: str) -> None:
    worker._static_check(code)  # 不抛即通过


# ---------- 3. 两份实现不许漂移 ----------

def test_both_workers_agree_on_every_case() -> None:
    for code in BLOCKED:
        for module in WORKERS:
            with pytest.raises(module._SafetyError):
                module._static_check(code)
    for code in ALLOWED:
        for module in WORKERS:
            module._static_check(code)
    deep = "def run(inputs):\n    return inputs" + ".b" * 600
    for module in WORKERS:
        with pytest.raises(module._SafetyError):
            module._static_check(deep)
