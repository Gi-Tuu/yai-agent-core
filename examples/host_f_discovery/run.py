"""宿主 F 演示：能力缺口 -> 按需发现 -> 权限确认 -> 本轮即可用。

运行：.venv/Scripts/python.exe examples/host_f_discovery/run.py [可选任务文本]

本演示刻意用一个"脚本化分类模型"，**无需 API Key、结果完全确定**：
第 1 次调用（路由分类）报告"缺少天气查询能力"，随后内核从静态目录发现
``get_weather`` 并注册；第 2 次调用模型时新工具已在工具清单里，模型直接
调用它；第 3 次调用给出最终答复。

真实模型（DeepSeek 等 OpenAI 兼容后端）下，只要模型在分类时输出
``missing_capability``，同一条闭环同样成立——tests/test_discovery_catalog.py
已用脚本模型离线覆盖了全部路径（含无候选、拒绝授权、发现源异常）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/

from runner_common import bootstrap, stream  # noqa: E402

bootstrap()

from host_f_discovery import capabilities as cap  # noqa: E402
from yai_core import (  # noqa: E402
    AgentCore,
    DiscoveredCandidate,
    ModelResponse,
    StaticCatalog,
    ToolCallRequest,
    build_spec,
)

DEFAULT_TASK = "我下午要出门，帮我查一下湛江的天气"


class GapDiscoveryModel:
    """离线确定性模型：先报缺口，再调用被发现的新工具，最后收尾。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            # 路由分类调用：判定现有工具不足，报告缺失能力。
            return ModelResponse(
                content=(
                    '{"strategy":"react","tier":"standard",'
                    '"reason":"需要查询天气，现有工具无法完成",'
                    '"missing_capability":"天气查询能力"}'
                )
            )
        if self.calls == 2:
            # 此时 get_weather 已在本轮被发现并注册，出现在 tools 清单里。
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="w1", name="get_weather", arguments={"city": "湛江"}
                    )
                ],
            )
        return ModelResponse(content="湛江当前晴，27℃，下午出门不用带伞。")


async def main() -> None:
    # 可选能力目录：命中关键词才被发现；目录里没有的能力永远不会被发现。
    catalog = StaticCatalog(
        [
            DiscoveredCandidate(
                cap.get_weather,
                ("天气", "气温", "下雨", "weather"),
                description="查询指定城市的实时天气（按需启用）",
            )
        ]
    )

    # 只注册默认能力（不用 auto，避免把可选能力提前注册）；目录单独注入。
    model = GapDiscoveryModel()
    core = AgentCore(model, llm_router=True, discovery=catalog)
    core.register_tools([build_spec(cap.list_tasks), build_spec(cap.add_task)])

    task = " ".join(sys.argv[1:]).strip() or DEFAULT_TASK
    await stream(core, task, "离线确定性模型（演示能力发现闭环）")


if __name__ == "__main__":
    asyncio.run(main())
