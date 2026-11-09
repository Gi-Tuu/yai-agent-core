"""Phase 2 演示：拉起"文档产品"子进程 → 父核经本机 IPC 调它写流水 → 杀进程验证失败关闭。

    python examples/host_m_federation/ipc_demo.py

零 Key、只走 127.0.0.1；发现文件写在临时目录，跑完即删。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if (_ROOT / "src" / "yai_core").is_dir():
    sys.path.insert(0, str(_ROOT / "src"))
if (_ROOT / "shell").is_dir():
    sys.path.insert(0, str(_ROOT))

from shell.federation import ChildUnavailable, CoreGrant, LinkChild  # noqa: E402

CHILD_SCRIPT = _ROOT / "examples" / "host_m_federation" / "child_app.py"


def _wait_discovery(
    directory: Path,
    node_id: str,
    process: subprocess.Popen,
    timeout: float = 20.0,
) -> Path:
    deadline = time.monotonic() + timeout
    path = directory / f"{node_id}.json"
    while time.monotonic() < deadline:
        if path.is_file():
            return path
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"子核进程提前退出：{stderr}")
        time.sleep(0.05)
    raise TimeoutError(f"等不到子核发现文件：{path}")


async def drive(discovery: Path, process: subprocess.Popen) -> None:
    link = await LinkChild.connect(discovery, parent_id="atm")
    print(f"[IPC] 连上子核 {link.node_id}（{link.child_id}），工具：{sorted(link.manifest())}")
    grant = CoreGrant(
        edge="atm->doc",
        child_id=link.child_id,
        tools=frozenset(link.manifest()),
    )
    result = await link.call(
        "add_entry",
        {"title": "IPC 流水 0001", "body": "转出 100 元"},
        grant=grant,
    )
    print(f"[IPC] 调用成功：ok={result.get('ok')} result={result.get('result')}")
    print(f"[IPC] 健康检查：{await link.health()}")
    await link.close()

    process.terminate()
    process.wait(timeout=10)
    # 子核退出后再连：必须是"失败关闭"，不是静默成功。
    try:
        await LinkChild.connect(discovery, parent_id="atm")
    except ChildUnavailable as exc:
        print(f"[IPC] 子核退出后调用失败（预期）：{type(exc).__name__}")
        return
    raise AssertionError("子核退出后仍然连上了——失败关闭被破坏")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        process = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                str(CHILD_SCRIPT),
                "--discovery-dir",
                tmp,
                "--parent-id",
                "atm",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            discovery = _wait_discovery(directory, "doc", process)
            asyncio.run(drive(discovery, process))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
