"""宿主 C 演示：同一份 Core 零修改，嵌入一个 AI 陪伴应用（AMBRACE 预演）。

运行：.venv/Scripts/python.exe examples/host_c_companion/run.py [可选任务文本]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/

from runner_common import bootstrap, build_model, load_dotenv, stream  # noqa: E402

bootstrap()

from host_c_companion import capabilities  # noqa: E402
from yai_core import AgentCore  # noqa: E402

DEFAULT_TASK = "先查看小拥的状态，然后检索和约定有关的记忆，给出主动关怀建议"


def build_core(model) -> AgentCore:
    """装配陪伴宿主：把当前档案（心情/能量/羁绊）注册成瞬态状态源。

    默认任务里"先查看小拥的状态"不必再调一次工具：模型每次调用前都会拿到
    最新档案，且不写入历史、不占长期记忆。
    """
    core = AgentCore.auto(capabilities, model, llm_router="auto")
    core.register_state_provider("companion", capabilities.get_companion_profile)
    return core


async def main() -> None:
    load_dotenv()
    model, backend = build_model()
    # llm_router="auto"：有 Key（真实模型）走 LLM 分类，无 Key（离线脚本模型）走规则。
    core = build_core(model)
    task = " ".join(sys.argv[1:]).strip() or DEFAULT_TASK
    await stream(core, task, backend)


if __name__ == "__main__":
    asyncio.run(main())
