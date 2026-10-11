"""端到端 Agent 评测 runner：真跑 ``AgentCore.astream``，量化完成率与工具正确性。

与 ``benchmark_router.py``（只评路由策略、用仿真事件）不同，本脚本用
:class:`yai_core.llm.scripted.ScriptedModel` 扮演模型，**真实驱动完整 AgentCore**
（路由 → 工具调用 → 权限 → 执行 → 结果回灌 → 收尾），再用内核 ``evaluation``
包对事件流打分。全程离线、零网络、零 API Key、结果确定。

用途：

- **回归基线**：golden cases 模拟"正确模型"，应当全部通过；改内核 / 改配置后
  若有 case 失败，说明行为退化；
- **真机评测的模板**：把 ScriptedModel 换成 OpenAICompatProvider（``[llm]``），
  同一套 golden 任务即可量化真实模型的多步完成率、工具正确率、越权与无支撑断言。

用法：
    python scripts/eval_agent.py                 # 跑全部 golden cases
    python scripts/eval_agent.py --gate 0.8      # 完成率低于 0.8 才以非零退出
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 脚本可直接运行：把 src/ 与 scripts/ 加入导入路径。
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

from eval_tasks import CASES, host_tool_specs  # noqa: E402

from yai_core import AgentCore  # noqa: E402
from yai_core.evaluation import (  # noqa: E402
    EvalResult,
    aggregate,
    collect_trace,
    score_task,
)
from yai_core.llm.scripted import ScriptedModel  # noqa: E402


async def run_case(golden, responses, max_iters: int) -> EvalResult:
    """构造一个带评测宿主工具的 Core，跑完一条 golden case 并评分。"""

    model = ScriptedModel(responses)
    core = AgentCore(
        model,
        llm_router=False,          # 纯规则路由：离线、确定
        auto_approve_tools=True,   # 基线不模拟权限拒绝（越权拦截另由测试覆盖）
        max_iters=max_iters,
    )
    # 注册评测宿主的全部工具（含高危工具，作为"诱惑"，正确模型不应调用）。
    core.registry.register_many(host_tool_specs())

    events = [event async for event in core.astream(golden.task)]
    trace = collect_trace(events)
    trace.model_calls = model.calls  # 回填真实模型调用次数
    score = score_task(golden, trace)
    return golden, trace, score


async def run_all(max_iters: int) -> list[EvalResult]:
    """顺序跑完所有 golden case（顺序执行，输出顺序稳定）。"""

    return [await run_case(golden, responses, max_iters)
            for golden, responses in CASES]


def print_table(results: list[EvalResult]) -> None:
    """打印逐任务结果表。"""

    print("\n=== 端到端 Agent 评测（golden 回归基线）===")
    header = f"{'任务ID':<26}{'结果':>6}{'工具精确':>9}{'工具召回':>9}{'工具数':>7}{'模型数':>7}"
    print(header)
    print("-" * len(header))
    for golden, _, score in results:
        verdict = "PASS" if score.passed else "FAIL"
        print(
            f"{golden.id:<26}{verdict:>6}"
            f"{score.tool_precision * 100:>8.0f}%"
            f"{score.tool_recall * 100:>8.0f}%"
            f"{score.n_tool_calls:>7}{score.n_model_calls:>7}"
        )
        # 失败时列出具体问题，便于定位。
        problems: list[str] = []
        if score.missing_tools:
            problems.append(f"漏调 {score.missing_tools}")
        if score.unexpected_tools:
            problems.append(f"多余调用 {score.unexpected_tools}")
        if score.overreach:
            problems.append(f"越权 {score.overreach}")
        if score.unsupported_claims:
            problems.append(f"无支撑断言 {score.unsupported_claims}")
        if score.missing_evidence:
            problems.append(f"漏交付凭证 {score.missing_evidence}")
        if problems:
            print("       -> " + "；".join(problems))


def main(gate: float, max_iters: int, out_dir: Path | None) -> dict:
    """跑评测、写 JSON、打印表格，并按 --gate 做硬校验。"""

    results = asyncio.run(run_all(max_iters))
    summary = aggregate(results)

    out_dir = out_dir or (_ROOT / "benchmark_out")
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": summary.to_dict(),
        "tasks": [score.to_dict() for _, _, score in results],
    }
    (out_dir / "eval_agent.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print_table(results)
    print("\n=== 汇总 ===")
    print(f"完成率 {summary.completion_rate * 100:.1f}%（{summary.passed}/{summary.n}）")
    print(f"工具精确率 {summary.tool_precision * 100:.1f}%，"
          f"工具召回率 {summary.tool_recall * 100:.1f}%")
    print(f"越权 {summary.overreach_count} 次，无支撑断言 "
          f"{summary.unsupported_claim_count} 处")
    print(f"平均工具数 {summary.avg_tool_calls:.2f}，"
          f"平均模型调用 {summary.avg_model_calls:.2f}")
    print(f"产物：{out_dir / 'eval_agent.json'}")

    if summary.completion_rate + 1e-9 < gate:
        print(f"\nFAIL：完成率 {summary.completion_rate:.2%} 低于闸门 {gate:.2%}，"
              "golden 回归未通过，请排查。")
        raise SystemExit(1)
    print(f"\nPASS：完成率达到闸门 {gate:.2%}。")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="端到端 Agent 离线评测")
    parser.add_argument("--gate", type=float, default=1.0,
                        help="完成率硬闸门，默认 1.0（golden 基线应全通过）")
    parser.add_argument("--max-iters", type=int, default=6)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    main(gate=args.gate, max_iters=args.max_iters, out_dir=args.out)
