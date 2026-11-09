"""文档产品侧（Phase 2 双进程 demo）：子核 + 本机 IPC 服务。

由 `examples/host_m_federation/ipc_demo.py` 拉起；也可以手动跑：

    python examples/host_m_federation/child_app.py --discovery-dir <目录> --parent-id atm
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if (_ROOT / "src" / "yai_core").is_dir():
    sys.path.insert(0, str(_ROOT / "src"))
if (_ROOT / "shell").is_dir():
    sys.path.insert(0, str(_ROOT))

from shell.federation import ChildCore, ChildServer  # noqa: E402

from yai_core import AgentCore, ModelResponse, ScriptedModel, build_spec  # noqa: E402

DOCUMENT: list[tuple[str, str]] = []


def add_entry(title: str, body: str = "") -> str:
    """向文档追加一条记录。"""
    DOCUMENT.append((title, body))
    return f"已写入「{title}」"


def list_entries() -> list[str]:
    """列出文档里的记录标题。"""
    return [title for title, _ in DOCUMENT]


async def serve(discovery_dir: Path, parent_id: str, node_id: str) -> None:
    doc_core = AgentCore(ScriptedModel([ModelResponse(content="idle")]))
    doc_core.register_tools([build_spec(add_entry), build_spec(list_entries)])
    child = ChildCore(doc_core, node_id=node_id, child_id="doc-app@0.29", product="doc")
    server = ChildServer(
        child,
        discovery_dir=discovery_dir,
        parent_tools={parent_id: frozenset({"add_entry", "list_entries"})},
    )
    info = await server.start()
    print(f"READY {info.host}:{info.port}", flush=True)
    try:
        await asyncio.Event().wait()  # 一直服务，直到被终止
    finally:
        await server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="文档子核（本机 IPC）")
    parser.add_argument("--discovery-dir", required=True)
    parser.add_argument("--parent-id", default="atm")
    parser.add_argument("--node-id", default="doc")
    args = parser.parse_args()
    try:
        asyncio.run(serve(Path(args.discovery_dir), args.parent_id, args.node_id))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
