"""宿主 H 命令行演示：YAI 嵌入真实开源项目 sherlock（92.5k stars, MIT）。

运行：.venv/Scripts/python.exe examples/host_h_sherlock/run.py
     .venv/Scripts/python.exe examples/host_h_sherlock/run.py "查 gi-tuu 的社交账号"

有 OPENAI_API_KEY 时自动走真实模型（真联网查 sherlock）；
没有 Key 时回退离线脚本模型，结果确定、不联网。
本演示的说服力在于：sherlock 是别人的 9 万星开源项目，我们没改它一行源码，
只在外面包了一个函数，Core 就用自然语言把它调起来了。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/

from runner_common import bootstrap, build_model, load_dotenv, stream  # noqa: E402

bootstrap()
load_dotenv()

from host_h_sherlock import capabilities as cap  # noqa: E402
from host_h_sherlock.demo_agent import DEFAULT_TASK, build_core  # noqa: E402
from yai_core import AgentCore, build_spec  # noqa: E402


def _make_core() -> tuple[AgentCore, str]:
    """有 Key 用真实模型，否则离线脚本模型。返回 (core, 后端标签)。"""
    if os.getenv("OPENAI_API_KEY"):
        model, label = build_model()
        core = AgentCore(model, auto_approve_tools=True)
        core.register_tools([
            build_spec(cap.list_available_sites),
            build_spec(cap.lookup_username),
        ])
        return core, label
    return build_core(), "离线脚本模型（未检测到 OPENAI_API_KEY）"


async def main() -> None:
    core, label = _make_core()
    task = " ".join(sys.argv[1:]).strip() or DEFAULT_TASK
    await stream(core, task, f"嵌入真实开源项目 sherlock · {label}")


if __name__ == "__main__":
    asyncio.run(main())
