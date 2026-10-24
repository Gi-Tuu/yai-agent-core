"""红线 1：内核本体零第三方硬依赖——**全库**扫描，不只盯一个文件。

背景：这条红线此前只有 `tests/test_mcp_bridge.py` 里的
`test_no_toplevel_mcp_import_keeps_kernel_dependency_free` 一个守卫，
而它只检查 `integrations/mcp/client.py` 一个文件，且整个模块顶部
`pytest.importorskip("mcp")`——没装 ``[mcp]`` extra 的环境里整条守卫被跳过。
实测把 `import httpx` 注入 `src/yai_core/core.py` / `tools/executor.py` 顶层，
1564 项测试全绿，红线被破坏而无人察觉。

本文件把守卫扩到 `src/yai_core` 全量：任何模块**顶层**出现非标准库、
非 `yai_core` 内部包的 import，都必须让本文件变红。
懒加载（函数体内 import）是允许的——那正是可选依赖的正确用法。
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
KERNEL = ROOT / "src" / "yai_core"

#: 允许在模块顶层出现的包前缀：标准库 + 内核自身 + 两个 typing 垫片。
ALLOWED_TOP_LEVEL = {"yai_core", "typing_extensions", "typing_inspect"}


def _stdlib_names() -> set[str]:
    return set(sys.stdlib_module_names)


def _module_level_imports(tree: ast.Module) -> list[tuple[str, int]]:
    """返回模块**顶层**（非函数体内）的 import 顶层包名与行号。"""
    found: list[tuple[str, int]] = []
    for node in tree.body:  # 只看模块顶层语句，不递归进函数/类体
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.append((node.module.split(".")[0], node.lineno))
    return found


def _kernel_files() -> list[pathlib.Path]:
    return sorted(KERNEL.rglob("*.py"))


def test_kernel_directory_exists() -> None:
    """前置：路径没写错，否则下面的扫描会"因为没扫到文件"而空转通过。"""
    files = _kernel_files()
    assert len(files) > 50, f"内核源码只找到 {len(files)} 个 .py，路径可能不对"


def test_no_module_level_third_party_import_in_kernel() -> None:
    """内核本体不得在模块顶层 import 任何第三方包。"""
    stdlib = _stdlib_names()
    violations: list[str] = []
    for path in _kernel_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, lineno in _module_level_imports(tree):
            if name in stdlib or name in ALLOWED_TOP_LEVEL:
                continue
            rel = path.relative_to(ROOT).as_posix()
            violations.append(f"{rel}:{lineno} 顶层 import {name}")
    assert not violations, "内核本体出现模块级第三方依赖（破坏零硬依赖红线）：\n" + "\n".join(
        violations
    )


def test_lazy_third_party_imports_are_allowed() -> None:
    """反证：函数体内的懒加载**必须**仍然允许（否则这条守卫会把正确写法也判死）。

    拿真实的懒加载模块做样本，确认顶层干净、函数体内确有第三方 import。
    """
    sample = KERNEL / "integrations" / "mcp" / "client.py"
    tree = ast.parse(sample.read_text(encoding="utf-8"))
    stdlib = _stdlib_names()
    top_third_party = [
        name
        for name, _ in _module_level_imports(tree)
        if name not in stdlib and name not in ALLOWED_TOP_LEVEL
    ]
    assert not top_third_party, f"样本文件顶层竟有第三方 import：{top_third_party}"
    nested: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Import):
                    nested.extend(a.name.split(".")[0] for a in inner.names)
                elif isinstance(inner, ast.ImportFrom) and inner.level == 0 and inner.module:
                    nested.append(inner.module.split(".")[0])
    assert "mcp" in nested, "样本文件里找不到函数体内的 mcp 懒加载，测试前提失效"


def test_guard_detects_injected_third_party_import() -> None:
    """守卫自身必须有效：构造一个含顶层第三方 import 的模块，扫描器要能抓到它。"""
    injected = ast.parse("import httpx\nfrom openai import OpenAI\n")
    names = [n for n, _ in _module_level_imports(injected)]
    assert "httpx" in names and "openai" in names
    # 标准库与内核自身不得被误报
    ok = ast.parse("import asyncio\nimport json\nfrom yai_core.types import AgentEvent\n")
    for name, _ in _module_level_imports(ok):
        assert name in _stdlib_names() or name in ALLOWED_TOP_LEVEL, name


def test_project_declares_no_runtime_dependencies() -> None:
    """红线 1 的另一半：`pyproject.toml` 的 `dependencies` 必须保持为空。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"].get("dependencies", [])
    assert deps == [], f"内核本体的 dependencies 必须为空，实际为 {deps}"


def test_optional_extras_are_not_imported_at_kernel_top_level() -> None:
    """可选 extra 的包不得被内核顶层 import（它们只能在 batteries/integrations 里懒加载）。"""
    stdlib = _stdlib_names()
    banned = {
        "openai",
        "anthropic",
        "fastapi",
        "uvicorn",
        "pydantic",
        "httpx",
        "yaml",
        "mcp",
        "numpy",
        "onnxruntime",
        "tokenizers",
        "cryptography",
        "pypdf",
        "opentelemetry",
    }
    violations: list[str] = []
    for path in _kernel_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, lineno in _module_level_imports(tree):
            if name in banned and name not in stdlib:
                violations.append(f"{path.relative_to(ROOT).as_posix()}:{lineno} import {name}")
    assert not violations, "可选依赖出现在内核顶层：\n" + "\n".join(violations)


@pytest.mark.parametrize("extra", ["llm", "server", "mcp", "openapi", "otel", "office"])
def test_optional_extras_are_declared(extra: str) -> None:
    """可选依赖必须挂在 pyproject 的 optional-dependencies 下，而不是主依赖。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"].get("optional-dependencies", {})
    assert extra in extras, f"缺少 [{extra}] 可选依赖组"
    assert data["project"].get("dependencies", []) == []
