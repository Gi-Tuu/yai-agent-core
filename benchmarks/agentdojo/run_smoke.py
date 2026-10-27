"""AgentDojo 单任务冒烟：验证模型、工具、评分链路，并测单例调用预算。

用法（在仓库根运行）：
  benchmark_out/agentdojo/.venv/Scripts/python.exe benchmarks/agentdojo/run_smoke.py
可选：
  --suite banking --user-task user_task_0 --injection-task injection_task_0
  --attack important_instructions --no-guard
"""

# ruff: noqa: E402, I001  (sys.path 注入必须在 agentdojo/yai_pipeline 导入之前)

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentdojo.agent_pipeline.agent_pipeline import load_system_message
from agentdojo.attacks.attack_registry import load_attack
from agentdojo.benchmark import run_task_with_injection_tasks, run_task_without_injection_tasks
from agentdojo.logging import OutputLogger
from agentdojo.models import MODEL_NAMES
from agentdojo.task_suite.load_suites import get_suite

from yai_core.types import EventType
from yai_pipeline import YAIPipeline

# AgentDojo 的攻击模板需要从 pipeline 名里认出一个"模型名"。补一条 DeepSeek 映射，
# 让 important_instructions 的攻击文本能正确称呼被测模型。
MODEL_NAMES.setdefault("deepseek", "DeepSeek")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default="banking")
    parser.add_argument("--user-task", default="user_task_0")
    parser.add_argument("--injection-task", default="injection_task_0")
    parser.add_argument("--attack", default="important_instructions")
    parser.add_argument("--no-guard", action="store_true")
    parser.add_argument("--benchmark-version", default="v1.2.2")
    parser.add_argument("--skip-clean", action="store_true")
    parser.add_argument("--skip-injection", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        print("缺少 OPENAI_API_KEY（.env）", file=sys.stderr)
        return 2

    pipeline = YAIPipeline(
        api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL"),
        model=os.getenv("LLM_MODEL", "deepseek-chat"),
        strong_model=os.getenv("LLM_STRONG_MODEL"),
        system_context=load_system_message(None),
        tool_output_guard=not args.no_guard,
    )
    suite = get_suite(args.benchmark_version, args.suite)
    user_task = suite.get_user_task_by_id(args.user_task)
    print(f"pipeline={pipeline.name}")
    print(f"suite={suite.name} user_task={args.user_task}")
    print(f"prompt={user_task.PROMPT}")

    logdir = ROOT / "benchmark_out" / "agentdojo" / "runs"
    logdir.mkdir(parents=True, exist_ok=True)
    with OutputLogger(str(logdir), live=None):
        if not args.skip_clean:
            started = time.perf_counter()
            utility, _ = run_task_without_injection_tasks(
                suite, pipeline, user_task, logdir, True, args.benchmark_version
            )
            elapsed = time.perf_counter() - started
            print(f"[clean] utility={utility} elapsed={elapsed:.1f}s")
            result = pipeline.last_result
            if result is not None:
                model_calls = sum(
                    1 for e in result.events if e.type == EventType.MODEL_MESSAGE
                )
                tool_calls = [
                    e.data.get("tool")
                    for e in result.events
                    if e.type == EventType.TOOL_CALL
                ]
                print(
                    f"[clean] strategy={result.strategy} model_calls={model_calls} "
                    f"tools={tool_calls}"
                )
                print(f"[clean] final={result.final_text[:600]!r}")
                for call in pipeline.last_recorded:
                    if call.error:
                        print(f"[clean] tool_error={call.name}: {call.error[:300]}")

        if not args.skip_injection:
            attack = load_attack(args.attack, suite, pipeline)
            started = time.perf_counter()
            utility_map, security_map = run_task_with_injection_tasks(
                suite,
                pipeline,
                user_task,
                attack,
                logdir,
                True,
                injection_tasks=[args.injection_task],
                benchmark_version=args.benchmark_version,
            )
            elapsed = time.perf_counter() - started
            print(f"[injection] attack={args.attack} elapsed={elapsed:.1f}s")
            print(f"[injection] utility={utility_map}")
            print(f"[injection] security={security_map}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
