"""30 秒 QuickStart：把两个普通函数交给内核，看它自己选策略、调工具、收尾。

零依赖、零 API Key、离线可跑（ScriptedModel 按脚本扮演模型）：

    python examples/quickstart.py

真实上线只换一行模型：``model = OpenAICompatProvider()``（见 README）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 从仓库直接运行时把 src/ 放进路径；pip 安装后这几行是无害的空操作。
_ROOT = Path(__file__).resolve().parents[1]
if (_ROOT / "src" / "yai_core").is_dir():
    sys.path.insert(0, str(_ROOT / "src"))

from yai_core import (  # noqa: E402 - 路径引导之后再导入
    AgentCore,
    EventType,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    build_spec,
)


def search_notes(keyword: str) -> list[str]:
    """搜索宿主笔记库。"""  # 你自己的普通业务函数，没有任何 Agent 代码
    return {"周报": ["Core 骨架", "工具调用", "事件流"]}.get(keyword, [])


def archive_note(title: str) -> str:
    """把一条笔记归档到「本周」目录。"""  # 写操作：宿主声明审批后内核会先要授权
    return f"已归档：{title}"


# 离线确定性模型：先搜、再归档、最后总结。真实环境换成 OpenAICompatProvider() 即可。
MODEL = ScriptedModel(
    [
        ModelResponse(
            content="",
            tool_calls=[
                ToolCallRequest(id="c1", name="search_notes", arguments={"keyword": "周报"})
            ],
        ),
        ModelResponse(
            content="",
            tool_calls=[
                ToolCallRequest(id="c2", name="archive_note", arguments={"title": "Core 骨架"})
            ],
        ),
        ModelResponse(content="本周周报 3 条，已归档「Core 骨架」。"),
    ]
)


async def main() -> None:
    core = AgentCore(MODEL)
    # 内核读取 type hints + docstring，自动生成工具 schema，不需要手写 Agent 代码。
    core.register_tools([build_spec(search_notes), build_spec(archive_note)])

    print("工具（自动内省）：", [tool["name"] for tool in core.list_tools()])
    async for event in core.astream("搜索周报，把第一条归档"):
        if event.type == EventType.STRATEGY_SELECTED:
            print(f"[路由] 选定策略：{event.data['strategy']}")
        elif event.type == EventType.PLAN_CREATED:
            print(f"[计划] {event.data['steps']}")
        elif event.type == EventType.TOOL_CALL:
            print(f"[工具] {event.data['tool']}({event.data['arguments']})")
        elif event.type == EventType.DONE:
            print(f"[完成] {event.data['final_text']}")


if __name__ == "__main__":
    asyncio.run(main())
