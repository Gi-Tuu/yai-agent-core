"""读取 run_bench.py 的结果 JSON，输出汇总表、Wilson 95% 区间与失败案例抽样。

用法：
  python analyze.py benchmark_out/agentdojo/results/xxx.json
  python analyze.py results.json --examples 5
"""

from __future__ import annotations

import argparse
import glob
import json
import math
from pathlib import Path


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def fmt_ci(successes: int, total: int) -> str:
    lo, hi = wilson(successes, total)
    return f"[{lo * 100:.1f}%, {hi * 100:.1f}%]"


def classify_counts(rows: list[dict]) -> dict[str, int]:
    """逐用例四分类计数（防御成功 / 注入达成 / 任务失败 / 无注入）。"""
    counts = {
        "injection_achieved": 0,
        "defended_task_done": 0,
        "defended_task_failed": 0,
        "no_injection": 0,
    }
    for row in rows:
        injection = row.get("injection_task")
        if not injection:
            counts["no_injection"] += 1
        elif row.get("attack_success"):
            counts["injection_achieved"] += 1
        elif row.get("utility"):
            counts["defended_task_done"] += 1
        else:
            counts["defended_task_failed"] += 1
    return counts


def load_cases(
    log_root: Path,
    pipeline: str,
    suite: str,
    user_task: str,
    attack: str,
    injection: str,
):
    path = log_root / pipeline / suite / user_task / attack / f"{injection}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result", nargs="?", default="")
    parser.add_argument("--examples", type=int, default=3)
    parser.add_argument("--log-root", default="benchmark_out/agentdojo/runs")
    args = parser.parse_args()

    if args.result:
        path = Path(args.result)
    else:
        candidates = sorted(
            glob.glob("benchmark_out/agentdojo/results/*.json"),
            key=lambda p: Path(p).stat().st_mtime,
        )
        if not candidates:
            print("没有结果 JSON")
            return 1
        path = Path(candidates[-1])
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"result: {path}")
    print(f"pipeline: {data.get('pipeline')}")
    print(f"attack: {data.get('attack')} guard={data.get('tool_output_guard')}")
    print()

    totals = {"clean": [0, 0], "inject_utility": [0, 0], "asr": [0, 0]}
    print("| suite | clean utility | utility under attack | ASR | cases |")
    print("|---|---|---|---|---|")
    for suite, payload in data["suites"].items():
        clean = payload.get("clean") or {}
        injected = payload.get("inject") or {}
        cases = injected.get("cases", 0)
        clean_n = clean.get("cases", 0)
        util_n = round((injected.get("utility") or 0) * cases) if cases else 0
        asr_n = round((injected.get("asr") or 0) * cases) if cases else 0
        if clean_n:
            totals["clean"][0] += round((clean.get("utility") or 0) * clean_n)
            totals["clean"][1] += clean_n
        if cases:
            totals["inject_utility"][0] += util_n
            totals["inject_utility"][1] += cases
            totals["asr"][0] += asr_n
            totals["asr"][1] += cases
        clean_hits = round((clean.get("utility") or 0) * clean_n) if clean_n else 0
        clean_cell = (
            f"{pct(clean.get('utility'))} ({fmt_ci(clean_hits, clean_n)})"
            if clean_n
            else "—"
        )
        util_cell = (
            f"{pct(injected.get('utility'))} ({fmt_ci(util_n, cases)})"
            if cases
            else "—"
        )
        asr_cell = f"{pct(injected.get('asr'))} ({fmt_ci(asr_n, cases)})" if cases else "—"
        print(
            f"| {suite} | {clean_cell} | {util_cell} | {asr_cell} | {cases} |"
        )
    print()
    for key, label in (
        ("clean", "clean utility"),
        ("inject_utility", "utility under attack"),
        ("asr", "ASR (attack success)"),
    ):
        n, total = totals[key]
        if total:
            print(f"{label}: {n}/{total} = {pct(n / total)}  Wilson95 {fmt_ci(n, total)}")

    # 失败/成功案例抽样
    log_root = Path(args.log_root)
    pipeline = data.get("pipeline")
    for suite, payload in data["suites"].items():
        cases = payload.get("cases") or []
        bad = [c for c in cases if c.get("attack_success")]
        util_fail = [c for c in cases if not c.get("utility")]
        print()
        print(
            f"== {suite}: attack_success={len(bad)}/{len(cases)} "
            f"utility_fail={len(util_fail)}/{len(cases)}"
        )
        for case in bad[: args.examples]:
            log = load_cases(
                log_root,
                pipeline,
                suite,
                case["user_task"],
                data.get("attack") or "none",
                case["injection_task"],
            )
            calls = []
            final = ""
            if log:
                for message in log.get("messages") or []:
                    for call in message.get("tool_calls") or []:
                        calls.append(call.get("function"))
                    if message.get("role") == "assistant" and not message.get("tool_calls"):
                        content = message.get("content")
                        if isinstance(content, list):
                            final = " ".join(
                                str(x.get("content", ""))
                                for x in content
                                if isinstance(x, dict)
                            )
            print(
                f"  [attack] {case['user_task']} × {case['injection_task']} "
                f"tools={calls[-6:]} final={final[:160]!r}"
            )
        for case in util_fail[: args.examples]:
            log = load_cases(
                log_root,
                pipeline,
                suite,
                case["user_task"],
                data.get("attack") or "none",
                case["injection_task"],
            )
            calls = []
            final = ""
            if log:
                for message in log.get("messages") or []:
                    for call in message.get("tool_calls") or []:
                        calls.append(call.get("function"))
                    if message.get("role") == "assistant" and not message.get("tool_calls"):
                        content = message.get("content")
                        if isinstance(content, list):
                            final = " ".join(
                                str(x.get("content", ""))
                                for x in content
                                if isinstance(x, dict)
                            )
            print(
                f"  [util-fail] {case['user_task']} × {case['injection_task']} "
                f"tools={calls[-6:]} final={final[:160]!r}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
