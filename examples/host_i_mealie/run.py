"""host_i Mealie 嵌入演示：命令行入口。

用法：
    .venv/Scripts/python.exe examples/host_i_mealie/run.py
    .venv/Scripts/python.exe examples/host_i_mealie/run.py "今晚做番茄炒蛋"

有 MEALIE_URL + MEALIE_TOKEN 时走真实 Mealie；否则用离线脚本模型。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_EXAMPLES = _HERE.parent
_SRC = _EXAMPLES.parent / "src"
for _p in (str(_EXAMPLES), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from host_i_mealie import capabilities as cap  # noqa: E402
from host_i_mealie.demo_agent import DEFAULT_TASK, build_core  # noqa: E402
from runner_common import load_dotenv  # noqa: E402
from yai_core import AgentCore, build_spec  # noqa: E402


def _make_core() -> tuple[AgentCore, str]:
    if os.getenv("MEALIE_URL") and os.getenv("MEALIE_TOKEN"):
        from runner_common import build_model as _bm

        model, label = _bm()
        core = AgentCore(model, auto_approve_tools=True)
        core.register_tools([
            build_spec(cap.search_recipes),
            build_spec(cap.get_recipe),
            build_spec(cap.get_mealplan),
            build_spec(cap.add_mealplan),
            build_spec(cap.get_shopping_list),
            build_spec(cap.add_shopping_item),
        ])
        return core, label
    return build_core(), "离线脚本模型（未配置 MEALIE_URL/TOKEN）"


async def _main(task: str) -> None:
    core, label = _make_core()
    print(f"[后端] {label}")
    result = await core.run(task)
    print()
    for e in result.events:
        if e.type.value == "tool_call":
            print(f"  → 调工具 {e.data.get('name')}")
        elif e.type.value == "tool_result":
            preview = str(e.data.get("preview") or "")[:80]
            print(f"  ← 结果 {preview}")
    print()
    print(result.final_text)


if __name__ == "__main__":
    load_dotenv()
    task = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TASK
    asyncio.run(_main(task))
