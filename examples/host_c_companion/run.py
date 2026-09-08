"""宿主 C 演示：同一份 Core 零修改，嵌入一个 AI 陪伴应用（AMBRACE 预演）。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo_model import OfflineScriptedModel  # noqa: E402
from host_c_companion import capabilities  # noqa: E402
from yai_core import AgentCore  # noqa: E402


async def main() -> None:
    if os.getenv("OPENAI_API_KEY"):
        from yai_core import OpenAICompatProvider

        model = OpenAICompatProvider()
    else:
        model = OfflineScriptedModel()
    core = AgentCore.auto(capabilities, model)
    print("自动发现的工具：", [t["name"] for t in core.list_tools()])
    result = await core.run("先查看小拥的状态，然后检索和约定有关的记忆，给出主动关怀建议")
    print("策略：", result.strategy.value)
    print("最终结果：", result.final_text)


if __name__ == "__main__":
    asyncio.run(main())
