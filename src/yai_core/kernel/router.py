"""Adaptive Router：把任务路由到 direct / react / plan / clarify。

v0.1 使用确定性规则（可测试、零成本、离线可跑）；
v0.2 增加 LLM 分类器（一次轻量模型调用，输出结构化策略 JSON），
     分类失败时自动回退到本规则实现——这就是"模型自适应"的兜底设计。
"""

from __future__ import annotations

from yai_core.tools.registry import ToolRegistry
from yai_core.types import Strategy

# 多步骤信号：出现时倾向先规划再执行
_PLAN_HINTS = (
    "然后", "接着", "之后", "再把", "分步", "步骤", "先", "并且", "同时",
    "对比", "整理成", "汇总成", "最终", "一共", "分别",
)
# 行动信号：需要调用工具
_ACTION_HINTS = (
    "查", "找", "搜", "列出", "统计", "计算", "导出", "获取", "读取",
    "记录", "新增", "整理", "分析", "筛选", "生成",
    # 显式的工具/MCP 调用意图（v0.2 接外部 MCP Server 后补齐）
    "问一下", "查询", "调用", "工具", "mcp",
)
# 过于模糊、需要反问
_CLARIFY_HINTS = ("随便", "你看着办", "什么都行", "帮我弄一下")


class AdaptiveRouter:
    def __init__(self, *, min_plan_steps: int = 2) -> None:
        self.min_plan_steps = min_plan_steps

    def classify(self, task: str, registry: ToolRegistry) -> Strategy:
        text = task.strip()
        if not text:
            return Strategy.CLARIFY
        if any(h in text for h in _CLARIFY_HINTS) and len(text) < 12:
            return Strategy.CLARIFY
        if len(registry) == 0:
            # 宿主没有任何能力 -> 只能直接回答
            return Strategy.DIRECT
        # 拉丁字母提示词（如 mcp）大小写不敏感；中文提示词按原文匹配。
        lowered = text.lower()
        wants_action = any(
            (h in lowered) if h.isascii() else (h in text) for h in _ACTION_HINTS
        )
        multi_step = sum(1 for h in _PLAN_HINTS if h in text) >= 1
        if multi_step and wants_action:
            return Strategy.PLAN
        if wants_action:
            return Strategy.REACT
        # 有工具但任务像闲聊/问答：直接答（模型仍可在 react 循环里自行决定用工具）
        return Strategy.DIRECT
