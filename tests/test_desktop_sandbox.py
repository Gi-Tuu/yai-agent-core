"""桌面灵动岛产品层子进程沙箱：真实子进程离线验证（不依赖网络 / Key / Qt）。

``shell/desktop/__init__.py`` 会连带导入 PySide6，CI 精简矩阵没有 Qt；
``sandbox.py`` 本身只用标准库 + yai_core，因此这里按文件路径直接加载该模块，
既验证产品沙箱、又不被包级 Qt 依赖拖累。沙箱完整隔离覆盖见
``tests/test_sandbox_example.py``（host_g 参考实现，逻辑一致）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_SANDBOX = ROOT / "shell" / "desktop" / "sandbox.py"


def _load_sandbox():
    spec = importlib.util.spec_from_file_location("yai_desktop_sandbox", _SANDBOX)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_worker_file_present() -> None:
    assert (_SANDBOX.parent / "_sandbox_worker.py").exists()


def test_desktop_sandbox_executes_code() -> None:
    mod = _load_sandbox()
    sb = mod.SubprocessSandbox()
    code = "def run(inputs):\n    return {'double': inputs['x'] * 2}"
    result = asyncio.run(sb.execute(code, {"x": 21}))
    assert result.ok, result.error
    assert result.output == {"double": 42}


def test_desktop_sandbox_blocks_import() -> None:
    mod = _load_sandbox()
    sb = mod.SubprocessSandbox()
    result = asyncio.run(
        sb.execute("import os\ndef run(inputs):\n    return 1", {})
    )
    assert not result.ok
    assert "ImportError" in result.error or "import" in result.error.lower()


def test_desktop_sandbox_blocks_dunder_reflection() -> None:
    """AST 闸门拒绝下划线属性：().__class__.__bases__ 在语法阶段被拦。"""
    mod = _load_sandbox()
    sb = mod.SubprocessSandbox()
    result = asyncio.run(
        sb.execute("def run(inputs):\n    return ().__class__.__bases__", {})
    )
    assert not result.ok
    assert "属性" in result.error or "__class__" in result.error


def test_desktop_sandbox_blocks_traceback_frame_escape() -> None:
    """回归 M1：异常帧遍历逃逸（e.__traceback__.tb_frame.f_globals）必须被拦。"""
    mod = _load_sandbox()
    sb = mod.SubprocessSandbox()
    code = (
        "def run(inputs):\n"
        "    try:\n"
        "        raise RuntimeError('x')\n"
        "    except Exception as e:\n"
        "        f = e.__traceback__.tb_frame\n"
        "        return f.f_globals.get('__name__')\n"
    )
    result = asyncio.run(sb.execute(code, {}))
    assert not result.ok
    assert "属性" in result.error or "__traceback__" in result.error
