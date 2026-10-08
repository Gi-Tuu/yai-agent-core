"""生产级 Docker 沙箱测试：离线安全配置核对 + 真机端到端（Docker 不可用则跳过）。

同 test_desktop_sandbox：按文件路径加载 docker_sandbox.py，避免 shell/desktop/__init__
连带导入 PySide6。真机用例需要 Docker 守护进程与 python:3.12-slim 镜像，CI 精简
矩阵无 Docker，自动 skip，不报错。
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_DOCKER_SANDBOX = ROOT / "shell" / "desktop" / "docker_sandbox.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("yai_docker_sandbox", _DOCKER_SANDBOX)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _docker_available() -> bool:
    """docker CLI 存在且守护进程可连通，才认为可跑真机用例。"""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=False
        ).returncode == 0
    except OSError:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="Docker 守护进程不可用（启动 Docker Desktop 后可跑）"
)


def test_entry_source_is_valid() -> None:
    mod = _load_module()
    assert mod._ENTRY_SOURCE.lstrip().startswith("from __future__")
    assert "def main" in mod._ENTRY_SOURCE


def test_argv_carries_hardening_flags() -> None:
    mod = _load_module()
    sb = mod.DockerSandbox()
    argv = sb._build_argv("yai-sandbox-test")
    # 关键隔离开关都在
    for flag in ["--rm", "-i", "--read-only", "--cap-drop", "--memory", "--cpus",
                 "--pids-limit", "no-new-privileges"]:
        assert flag in argv
    # 网络必须是 none（不是 host/默认桥接）
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    # 容器内以隔离模式跑 python，且用 -c 内联入口
    assert "python" in argv
    assert "-c" in argv


@requires_docker
def test_docker_sandbox_executes_code() -> None:
    mod = _load_module()
    sb = mod.DockerSandbox(timeout=180)  # 首次可能拉镜像，给宽超时
    code = "def run(inputs):\n    return {'double': inputs['x'] * 2}"
    result = asyncio.run(sb.execute(code, {"x": 21}))
    assert result.ok, result.error
    assert result.output == {"double": 42}


@requires_docker
def test_docker_sandbox_blocks_import() -> None:
    mod = _load_module()
    sb = mod.DockerSandbox(timeout=180)
    # 白名单 worker 无 import；叠加 --network none，双重断网/断模块
    result = asyncio.run(
        sb.execute("import socket\ndef run(inputs):\n    return 1", {})
    )
    assert not result.ok
