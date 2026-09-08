"""宿主 A 演示：同一个 Core 自动适配笔记应用。

运行：python examples/host_a_notes/run.py
有 OPENAI_API_KEY 时走真实模型；否则走离线脚本模型。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo_model import OfflineScriptedModel  # noqa: E402
from host_a_notes import capabilities  # noqa: E402
from yai_core import AgentCore  # noqa: E402


def build_core() -> AgentCore:
    if os.getenv("OPENAI_API_KEY"):
        from yai_core import OpenAICompatProvider  # noqa: E402

        model = OpenAICompatProvider()
    else:
        model = OfflineScriptedModel()
    # auto：内省 capabilities 模块，自动注册 list_notes/search_notes/count_notes
    return AgentCore.auto(capabilities, model)


async def main() -> None:
    core = build_core()
    print("自动发现的工具：", [t["name"] for t in core.list_tools()])
    result = await core.run("搜索笔记里和比赛有关的内容并统计数量")
    print("策略：", result.strategy.value)
    print("最终结果：", result.final_text)


if __name__ == "__main__":
    asyncio.run(main())
