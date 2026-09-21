"""host_g 的 Core 装配点（CLI 与网页共用的唯一 Agent 接线处）。

宿主是一个极简候选人应用：``capabilities.py`` 只暴露只读的
``list_candidates``，**没有**加权评分能力。这里把 YAI Agent Core 接进来，
配上 :class:`~host_g_sandbox.sandbox.SubprocessSandbox`，于是模型可以在
运行中现场 ``create_code_tool`` 造一个加权评分工具，并在隔离子进程里执行。

为了让演示无需 API Key、结果完全确定，这里用一个脚本化模型
（:class:`CreateCodeToolModel`）固定走"造工具 → 取数 → 沙箱执行 → 收尾"
四步；真实模型（DeepSeek / Agnes 等 OpenAI 兼容后端）只要在缺能力时调用
``create_code_tool``，同一条链路同样成立。

``run.py``（命令行）与 ``web_app.py``（网页壳）都只调用 :func:`build_core`，
不在各自文件里重复装配逻辑。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许脚本直接运行（python examples/host_g_sandbox/xxx.py）：补 examples/ 与 src/。
_EXAMPLES = Path(__file__).resolve().parents[1]
_SRC = Path(__file__).resolve().parents[2] / "src"
for _p in (str(_EXAMPLES), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from host_g_sandbox import capabilities as cap  # noqa: E402
from host_g_sandbox.sandbox import SubprocessSandbox  # noqa: E402
from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec  # noqa: E402

DEFAULT_TASK = "列出候选人，按技能 0.6、经验 0.4 算综合分，从高到低排序"

#: 模型现场生成的代码工具：定义 run(inputs)，只用到白名单内置（纯计算）。
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


def build_core(candidates: list[dict] | None = None) -> AgentCore:
    """装配一个全新的 Core 实例（沙箱 + 只读候选人工具，默认全放行）。

    每次调用都返回新实例，保证命令行 / 每个 HTTP 请求之间互不串状态。
    """
    model = CreateCodeToolModel(candidates or cap.SAMPLE_CANDIDATES)
    # 关键：传入宿主沙箱，内核才会启用 create_code_tool；默认全放行（离线演示）。
    core = AgentCore(
        model,
        sandbox=SubprocessSandbox(),
        auto_approve_tools=True,
    )
    core.register_tools([build_spec(cap.list_candidates)])
    return core
