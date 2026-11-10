"""示例子核 #1：笔记应用（独立运行，也可被父核经本机 IPC 调用）。

    python examples/child_cores/notes_app.py --discovery-dir <目录> --parent-id hub
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

NOTES: dict[str, list[str]] = {"周报": ["Core 骨架"]}


def add_note(keyword: str, text: str) -> dict:
    """在指定分类下新增一条笔记。"""
    NOTES.setdefault(keyword, []).append(text)
    return {"keyword": keyword, "count": len(NOTES[keyword])}


def search_notes(keyword: str) -> list[str]:
    """按分类关键词搜索笔记条目。"""
    return list(NOTES.get(keyword, []))


async def serve(discovery_dir: Path, parent_id: str, node_id: str) -> None:
    core = AgentCore(ScriptedModel([ModelResponse(content="idle")]))
    core.register_tools([build_spec(add_note), build_spec(search_notes)])
    child = ChildCore(core, node_id=node_id, child_id="notes-app@0.33", product="notes")
    server = ChildServer(
        child,
        discovery_dir=discovery_dir,
        parent_tools={parent_id: frozenset({"add_note", "search_notes"})},
    )
    info = await server.start()
    print(f"READY {info.host}:{info.port}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="笔记子核")
    parser.add_argument("--discovery-dir", required=True)
    parser.add_argument("--parent-id", default="hub")
    parser.add_argument("--node-id", default="notes")
    args = parser.parse_args()
    try:
        asyncio.run(serve(Path(args.discovery_dir), args.parent_id, args.node_id))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
