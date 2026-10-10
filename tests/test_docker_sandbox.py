"""生产级 Docker 沙箱测试：离线安全配置核对 + 真机端到端（Docker 不可用则跳过）。

同 test_desktop_sandbox：按文件路径加载 docker_sandbox.py，避免 shell/desktop/__init__
连带导入 PySide6。真机用例是 Linux 容器测试，需要 Linux 容器 daemon 与
python:3.12-slim 镜像：ubuntu CI 真正执行；windows CI 的 Docker 默认 Windows
容器（不支持 --read-only 等 Linux flags），自动 skip，不报错。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
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


def _docker_info_ok() -> bool:
    """docker CLI 存在且守护进程可连通。"""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=False
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        # docker CLI 存在但 daemon 冷启动慢（Windows Docker Desktop 常见）：
        # 超时视为"未就绪"，让真机用例 skip，而不是在收集阶段抛错中断。
        return False


def _docker_available() -> bool:
    """真机用例是 **Linux 容器**测试：daemon 可达且 OSType == linux 才跑。

    镜像 python:3.12-slim 与 --read-only / --cap-drop / --tmpfs 等加固项均为
    Linux 特性；Windows 容器模式不支持（如 "read-only mode is not supported
    for Windows containers"）。
    - GitHub windows-latest 的 Docker 默认 Windows 容器（OSType=windows）→ 跳过；
    - GitHub ubuntu-latest 预装 Linux Docker（OSType=linux）→ 真机端到端真正执行；
    - 本地 Docker Desktop 切到 Linux 容器并启动后可跑，否则跳过；
    - 读不到 OSType 的极旧版本：Linux 放行，Windows 需显式 YAI_DOCKER_LINUX=1。
    """
    if not _docker_info_ok():
        return False
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.OSType}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    ostype = (
        proc.stdout.strip().lower()
        if proc is not None and proc.returncode == 0
        else ""
    )
    if ostype:
        return ostype == "linux"
    return sys.platform != "win32" or os.environ.get("YAI_DOCKER_LINUX", "") == "1"


requires_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="需要 Linux 容器 Docker（启动 Docker Desktop 并切到 Linux 容器后可跑）",
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


def test_docker_gate_requires_linux_daemon(monkeypatch) -> None:
    """真机 gate 必须识别 daemon 容器 OS：只在 Linux 容器上跑，Windows 容器跳过。"""
    monkeypatch.setattr(
        shutil, "which",
        lambda name: r"C:\Program Files\Docker\docker.exe" if name == "docker" else None,
    )

    def fake_run_factory(ostype: str, info_ok: bool = True):
        def run(argv, **kwargs):
            def out(s: str):
                return s if kwargs.get("text") else s.encode()
            if argv[:2] == ["docker", "info"]:
                if not info_ok:
                    return subprocess.CompletedProcess(argv, 1, out("cannot connect"), b"")
                if "--format" in argv:
                    return subprocess.CompletedProcess(argv, 0, out(ostype), b"")
                return subprocess.CompletedProcess(argv, 0, out("Server Version: x"), b"")
            return subprocess.CompletedProcess(argv, 1, out("unknown"), b"")
        return run

    # Windows 容器模式：docker 可达也不跑（CI windows-latest 的情形）
    monkeypatch.setattr(subprocess, "run", fake_run_factory("windows"))
    assert _docker_available() is False
    # Linux 容器模式：跑（CI ubuntu-latest / 本地 Docker Desktop Linux 模式）
    monkeypatch.setattr(subprocess, "run", fake_run_factory("linux"))
    assert _docker_available() is True
    # daemon 不可达：不跑
    monkeypatch.setattr(subprocess, "run", fake_run_factory("linux", info_ok=False))
    assert _docker_available() is False


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
