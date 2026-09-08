"""宿主 B 演示：同一份 Core、零修改，自动适配销售数据应用。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo_model import OfflineScriptedModel  # noqa: E402
from host_b_data import capabilities  # noqa: E402
from yai_core import AgentCore  # noqa: E402


async def main() -> None:
    if os.getenv("OPENAI_API_KEY"):
        from yai_core import OpenAICompatProvider

        model = OpenAICompatProvider()
    else:
        model = OfflineScriptedModel()
    core = AgentCore.auto(capabilities, model)
    print("自动发现的工具：", [t["name"] for t in core.list_tools()])
    result = await core.run("先筛选硬件品类，然后统计销售额并整理成报告")
    print("策略：", result.strategy.value)
    print("最终结果：", result.final_text)


if __name__ == "__main__":
    asyncio.run(main())
