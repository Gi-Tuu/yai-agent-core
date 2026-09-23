"""host_i Mealie 的 Core 装配点（CLI 与网页共用的唯一 Agent 接线处）。

宿主包了 6 个 Mealie 工具；脚本化模型演示一条完整的"找菜→看食材→加购物清单"链：
1. search_recipes("番茄") → 搜到"番茄炒蛋"
2. get_recipe("tomato-egg") → 拿到食材清单
3. add_shopping_item("鸡蛋", "2 个") → 自动加进购物清单
4. 总结
"""

from __future__ import annotations

from host_i_mealie import capabilities as cap
from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec

DEFAULT_TASK = "今晚做番茄炒蛋，帮我找一下食谱并把缺的食材加进购物清单"


class ScriptedMealieModel:
    """离线确定性模型：搜食谱 → 看详情 → 加购物项 → 总结。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="i1", name="search_recipes",
                        arguments={"query": "番茄炒蛋", "limit": 5},
                    )
                ],
            )
        if self.calls == 2:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="i2", name="get_recipe",
                        arguments={"slug": "tomato-egg"},
                    )
                ],
            )
        if self.calls == 3:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="i3", name="add_shopping_item",
                        arguments={"food_name": "鸡蛋", "note": "2 个"},
                    )
                ],
            )
        return ModelResponse(
            content="已找到番茄炒蛋食谱，查看食材后把鸡蛋加入购物清单。"
                    "这就是 YAI 的多工具调用：Core 自己串起 搜菜 -> 看食材 -> 加清单 三步。"
        )


def build_core() -> AgentCore:
    """装配一个全新的 Core：注册 Mealie 六个工具，默认全放行（离线演示）。"""
    model = ScriptedMealieModel()
    core = AgentCore(model, auto_approve_tools=True)
    core.register_tools([
        build_spec(cap.search_recipes),
        build_spec(cap.get_recipe),
        build_spec(cap.get_mealplan),
        build_spec(cap.add_mealplan),
        build_spec(cap.get_shopping_list),
        build_spec(cap.add_shopping_item),
    ])
    return core
