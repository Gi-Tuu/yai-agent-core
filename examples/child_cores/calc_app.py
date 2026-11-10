"""示例子核 #2：计算器应用（独立运行，也可被父核经本机 IPC 调用）。

    python examples/child_cores/calc_app.py --discovery-dir <目录> --parent-id hub
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

HISTORY: list[str] = []


def add(a: float, b: float) -> dict:
    """计算两数之和。"""
    result = a + b
    HISTORY.append(f"{a}+{b}={result}")
    return {"result": result}


def multiply(a: float, b: float) -> dict:
    """计算两数之积。"""
    result = a * b
    HISTORY.append(f"{a}*{b}={result}")
    return {"result": result}


def history() -> list[str]:
    """返回本应用最近的计算记录。"""
    return list(HISTORY)


async def serve(discovery_dir: Path, parent_id: str, node_id: str) -> None:
    core = AgentCore(ScriptedModel([ModelResponse(content="idle")]))
    core.register_tools([build_spec(add), build_spec(multiply), build_spec(history)])
    child = ChildCore(core, node_id=node_id, child_id="calc-app@0.33", product="calc")
    server = ChildServer(
        child,
        discovery_dir=discovery_dir,
        parent_tools={parent_id: frozenset({"add", "multiply", "history"})},
    )
    info = await server.start()
    print(f"READY {info.host}:{info.port}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="计算器子核")
    parser.add_argument("--discovery-dir", required=True)
    parser.add_argument("--parent-id", default="hub")
    parser.add_argument("--node-id", default="calc")
    args = parser.parse_args()
    try:
        asyncio.run(serve(Path(args.discovery_dir), args.parent_id, args.node_id))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
