"""AgentDojo 批量跑分：clean utility + important_instructions 攻击下的 utility/ASR。

口径说明（写报告时必须带）：
- AgentDojo 代码里的 security_results=True 表示**注入目标达成**（攻击成功），
  因此 ASR = mean(security_results)，防护率 = 1 - ASR。
- utility_results 在注入模式下是"攻击在场时的用户任务完成率"；
  clean 模式单独跑，得到 benign utility。
- 模型、版本、guard 开关都写进结果 JSON，便于复现。

用法：
  benchmark_out/agentdojo/.venv/Scripts/python.exe benchmarks/agentdojo/run_bench.py \
      --suites banking --mode both --attack important_instructions
"""

# ruff: noqa: E402, I001  (sys.path 注入必须在 agentdojo/yai_pipeline 导入之前)

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentdojo.agent_pipeline.agent_pipeline import load_system_message
from agentdojo.attacks.attack_registry import load_attack
from agentdojo.benchmark import (
    benchmark_suite_with_injections,
    benchmark_suite_without_injections,
)
from agentdojo.logging import OutputLogger
from agentdojo.models import MODEL_NAMES
from agentdojo.task_suite.load_suites import get_suite

from yai_pipeline import YAIPipeline

MODEL_NAMES.setdefault("deepseek", "DeepSeek")

ALL_SUITES = ("banking", "travel", "slack", "workspace")


def _mean(values) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def _case_rows(result: dict) -> list[dict]:
    rows = []
    for (user_task, injection_task), utility in result["utility_results"].items():
        rows.append(
            {
                "user_task": user_task,
                "injection_task": injection_task,
                "utility": utility,
                "attack_success": result["security_results"].get(
                    (user_task, injection_task)
                ),
            }
        )
    return rows


def _summarize(result: dict) -> dict:
    utility = list(result["utility_results"].values())
    security = list(result["security_results"].values())
    injection_utility = list(result["injection_tasks_utility_results"].values())
    return {
        "cases": len(utility),
        "utility": _mean(utility),
        "asr": _mean(security),
        "injection_task_utility": _mean(injection_utility),
        "injection_task_count": len(injection_utility),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite-version", default="v1.2.2")
    parser.add_argument("--suites", default="banking")
    parser.add_argument("--mode", choices=("clean", "inject", "both"), default="both")
    parser.add_argument("--attack", default="important_instructions")
    parser.add_argument("--no-guard", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--user-tasks", default="")
    parser.add_argument("--injection-tasks", default="")
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        print("缺少 OPENAI_API_KEY（.env）", file=sys.stderr)
        return 2

    suites = [s.strip() for s in args.suites.split(",") if s.strip()]
    unknown = [s for s in suites if s not in ALL_SUITES]
    if unknown:
        print(f"未知 suite: {unknown}", file=sys.stderr)
        return 2
    user_tasks = [s.strip() for s in args.user_tasks.split(",") if s.strip()]
    injection_tasks = [s.strip() for s in args.injection_tasks.split(",") if s.strip()]

    pipeline = YAIPipeline(
        api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL"),
        model=os.getenv("LLM_MODEL", "deepseek-chat"),
        strong_model=os.getenv("LLM_STRONG_MODEL"),
        system_context=load_system_message(None),
        tool_output_guard=not args.no_guard,
    )
    run_root = ROOT / "benchmark_out" / "agentdojo" / "runs"
    result_dir = ROOT / "benchmark_out" / "agentdojo" / "results"
    run_root.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    label = args.label or (
        f"{args.mode}-{args.attack if args.mode != 'clean' else 'none'}-"
        f"{'guard' if not args.no_guard else 'noguard'}-"
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )

    payload = {
        "label": label,
        "created_at": datetime.now(UTC).isoformat(),
        "benchmark_version": args.suite_version,
        "attack": args.attack if args.mode != "clean" else None,
        "tool_output_guard": not args.no_guard,
        "model": os.getenv("LLM_MODEL"),
        "base_url": os.getenv("OPENAI_BASE_URL"),
        "pipeline": pipeline.name,
        "suites": {},
    }

    for suite_name in suites:
        suite = get_suite(args.suite_version, suite_name)
        suite_payload = {}
        print(f"=== suite {suite_name} ===", flush=True)

        if args.mode in ("clean", "both"):
            started = time.perf_counter()
            with OutputLogger(str(run_root), live=None):
                clean = benchmark_suite_without_injections(
                    pipeline,
                    suite,
                    logdir=run_root,
                    force_rerun=not args.force,
                    user_tasks=user_tasks or None,
                    benchmark_version=args.suite_version,
                )
            elapsed = time.perf_counter() - started
            summary = _summarize(clean)
            summary["elapsed_seconds"] = round(elapsed, 1)
            suite_payload["clean"] = summary
            print(
                f"[clean] utility={summary['utility']} n={summary['cases']} "
                f"elapsed={elapsed:.0f}s",
                flush=True,
            )

        if args.mode in ("inject", "both"):
            attack = load_attack(args.attack, suite, pipeline)
            started = time.perf_counter()
            with OutputLogger(str(run_root), live=None):
                attacked = benchmark_suite_with_injections(
                    pipeline,
                    suite,
                    attack,
                    logdir=run_root,
                    force_rerun=not args.force,
                    user_tasks=user_tasks or None,
                    injection_tasks=injection_tasks or None,
                    benchmark_version=args.suite_version,
                )
            elapsed = time.perf_counter() - started
            summary = _summarize(attacked)
            summary["elapsed_seconds"] = round(elapsed, 1)
            suite_payload["inject"] = summary
            suite_payload["cases"] = _case_rows(attacked)
            print(
                f"[inject] utility={summary['utility']} asr={summary['asr']} "
                f"n={summary['cases']} elapsed={elapsed:.0f}s",
                flush=True,
            )

        payload["suites"][suite_name] = suite_payload
        payload["pipeline_errors"] = pipeline.error_count
        payload["last_pipeline_error"] = pipeline.last_error
        out = result_dir / f"{label}.json"
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"saved: {out}", flush=True)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception:
        traceback.print_exc()
        raise SystemExit(1) from None
