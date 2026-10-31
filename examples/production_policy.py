"""生产环境权限配置的最小可运行示例（docs/production-permissions.md 的配套实现）。

与 serve_example 的区别：本示例显式配置权限策略（白名单 + 写操作审批 + 外发闸），
是被 tests/test_production_defaults.py 引用的"不会文档漂移"的模板。

运行：uv run python examples/production_policy.py
"""

from __future__ import annotations

import asyncio

from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec
from yai_core.policy import AllowlistPolicy
from yai_core.policy.approval import ApprovalPolicy, ApprovalRule
from yai_core.types import ToolGovernance


# 写操作（白名单外 → 走审批通道；金额超限由参数级审批拦）
def submit_expense(amount: int, purpose: str) -> str:
    """提交报销单。"""
    return f"报销单已提交：{purpose} {amount} 元"


# 读操作（白名单内 → 自动放行）
def list_expenses() -> list:
    """列出已有报销单。"""
    return [{"id": 1, "amount": 300}]


def build_core() -> AgentCore:
    from yai_core.channels import CollectChannel

    policy = AllowlistPolicy(("list_expenses",), mode="auto")
    submit_spec = build_spec(submit_expense)
    submit_spec.governance = ToolGovernance(
        approval=ApprovalPolicy(when=(ApprovalRule("amount", ">", 1000),)),
    )
    core = AgentCore(
        _OfflineModel(),
        policy=policy,
        auto_approve_tools=False,  # 显式声明：非 allow_all 开发形态
        channel=CollectChannel(),
    )
    core.register_tools([build_spec(list_expenses), submit_spec])
    return core


class _OfflineModel:
    """离线演示模型：先列后写。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="list_expenses", arguments={})],
            )
        return ModelResponse(content="报销单已列出，提交请走审批。")


async def main() -> None:
    core = build_core()
    result = await core.run("看一下报销单")
    print("final:", result.final_text)


if __name__ == "__main__":
    asyncio.run(main())
