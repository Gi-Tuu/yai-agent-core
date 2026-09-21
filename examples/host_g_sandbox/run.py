"""宿主 G 演示：模型现场"造"一个代码工具，在沙箱里执行。

运行：.venv/Scripts/python.exe examples/host_g_sandbox/run.py

本演示刻意用一个"脚本化模型"，**无需 API Key、结果完全确定**：
  1. 宿主只提供只读的 list_candidates，没有"加权评分"能力；
  2. 模型调用 meta-tool create_code_tool，现场生成 weighted_score；
  3. 内核登记工具（48h TTL），随后模型先取候选人、再调用 weighted_score；
  4. weighted_score 由宿主的 SubprocessSandbox 在隔离子进程里执行；
  5. 模型基于沙箱返回的分数给出最终排序。

真实模型（DeepSeek 等 OpenAI 兼容后端）下，只要模型在缺能力时调用
create_code_tool，同一条链路同样成立。tests/test_sandbox_example.py
用真实子进程（非 mock）覆盖了正常执行、超时、禁用 import/open 等路径。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/

from runner_common import bootstrap, stream  # noqa: E402

bootstrap()

from host_g_sandbox import capabilities as cap  # noqa: E402
from host_g_sandbox.sandbox import SubprocessSandbox  # noqa: E402
from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec  # noqa: E402

DEFAULT_TASK = "列出候选人，按技能 0.6、经验 0.4 算综合分，从高到低排序"

# 模型现场生成的代码工具：定义 run(inputs)，只用到白名单内置（纯计算）。
WEIGHTED_CODE = """
def run(inputs):
    candidates = inputs.get("candidates", [])
    weights = inputs.get("weights", {"skill": 0.6, "experience": 0.4})
    scored = []
    for c in candidates:
        item = dict(c)
        item["score"] = round(
            item["skill"] * weights["skill"]
            + item["experience"] * weights["experience"],
            2,
        )
        scored.append(item)
    return sorted(scored, key=lambda x: x["score"], reverse=True)
"""


class CreateCodeToolModel:
    """离线确定性模型：创建代码工具 → 取数 → 调用沙箱工具 → 收尾。"""

    def __init__(self, candidates: list[dict]) -> None:
        self.calls = 0
        self.candidates = candidates

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            # 宿主没有加权评分能力 → 现场创建一个代码工具。
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="create_code_tool",
                        arguments={
                            "name": "weighted_score",
                            "description": "按技能/经验权重给候选人计算综合分并降序排序",
                            "input_schema": {
                                "type": "object",
                                "properties": {
                                    "candidates": {"type": "array"},
                                    "weights": {"type": "object"},
                                },
                                "required": ["candidates"],
                            },
                            "code": WEIGHTED_CODE,
                        },
                    )
                ],
            )
        if self.calls == 2:
            # 先用宿主原生工具取数据。
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="c2", name="list_candidates", arguments={})
                ],
            )
        if self.calls == 3:
            # 调用刚创建的代码工具（在沙箱里执行）。
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c3",
                        name="weighted_score",
                        arguments={
                            "candidates": self.candidates,
                            "weights": {"skill": 0.6, "experience": 0.4},
                        },
                    )
                ],
            )
        return ModelResponse(
            content=(
                "按技能 0.6 + 经验 0.4 计算综合分，排序为："
                "林晓 81.6 > 周岚 81.0 > 陈默 80.0，建议优先考虑林晓。"
            )
        )


async def main() -> None:
    model = CreateCodeToolModel(cap.SAMPLE_CANDIDATES)
    # 关键：传入宿主沙箱，内核才会启用 create_code_tool；默认全放行（离线演示）。
    core = AgentCore(
        model,
        sandbox=SubprocessSandbox(),
        auto_approve_tools=True,
    )
    core.register_tools([build_spec(cap.list_candidates)])

    task = " ".join(sys.argv[1:]).strip() or DEFAULT_TASK
    await stream(core, task, "离线确定性模型 + 子进程沙箱（演示代码工具）")

    # 演示后台生命周期 API：查看代码工具状态。
    print("-" * 60)
    status = core.code_tools_status()
    print(f"代码工具注册表：启用={status['enabled']}，存活={status['live']}，明细：")
    for item in status["tools"]:
        print(f"   - {item['name']}（存活={item['live']}，"
              f"永久={item['permanent']}，调用次数={item['call_count']}，"
              f"TTL={item['ttl_seconds'] // 3600}h）")


if __name__ == "__main__":
    asyncio.run(main())
