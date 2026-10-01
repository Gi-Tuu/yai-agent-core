"""shell 代码工具仓库管理测试：不依赖 Qt / 网络 / Key。

``shell/desktop/__init__.py`` 会连带导入 PySide6，CI 精简矩阵没有 Qt；
``tool_vault.py`` 本身只用 yai_core，故按文件路径直接加载
（与 ``test_desktop_sandbox.py`` 同模式）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_VAULT = ROOT / "shell" / "desktop" / "tool_vault.py"


def _load_vault():
    spec = importlib.util.spec_from_file_location("yai_tool_vault", _VAULT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_BODY = {
    "name": "double",
    "description": "把输入数字翻倍",
    "input_schema": {
        "type": "object",
        "properties": {"x": {"type": "number"}},
        "required": ["x"],
    },
    "code": "def run(inputs):\n    return {'value': inputs['x'] * 2}",
}


def _seed(path: Path) -> None:
    # create 只注册、不执行，不需要沙箱。
    from yai_core import CodeToolManager, ToolRegistry

    CodeToolManager(ToolRegistry(), storage_path=path).create(**_BODY)


def test_vault_status_empty_when_no_file(tmp_path) -> None:
    mod = _load_vault()
    status = mod.vault_status(tmp_path / "nope.json")
    assert status["total"] == 0 and status["tools"] == []


def test_vault_status_lists_seeded(tmp_path) -> None:
    path = tmp_path / "c.json"
    _seed(path)
    status = _load_vault().vault_status(path)
    assert status["total"] == 1 and status["tools"][0]["name"] == "double"
    assert status["tools"][0]["description"] == _BODY["description"]


def test_vault_set_permanent_persists(tmp_path) -> None:
    mod = _load_vault()
    path = tmp_path / "c.json"
    _seed(path)
    assert mod.vault_set_permanent(path, "double", True) is True
    assert mod.vault_status(path)["tools"][0]["permanent"] is True
    assert mod.vault_set_permanent(path, "double", False) is True
    assert mod.vault_status(path)["tools"][0]["permanent"] is False


def test_vault_remove_deletes(tmp_path) -> None:
    mod = _load_vault()
    path = tmp_path / "c.json"
    _seed(path)
    assert mod.vault_remove(path, "double") is True
    assert mod.vault_status(path)["total"] == 0
    assert mod.vault_remove(path, "double") is False
