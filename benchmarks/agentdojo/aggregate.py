"""AgentDojo 跑分聚合：重复采样的统计与逐用例分类（纯函数，零 agentdojo 依赖）。

独立成模块的原因：run_bench.py 顶层 import agentdojo（只存在于评测专用 venv），
聚合逻辑要被主仓测试覆盖，就必须与那个依赖隔离。
"""

from __future__ import annotations

import statistics


def aggregate_runs(summaries: list[dict]) -> dict:
    """把 N 次重复运行的 summary 聚合成 mean ± std。

    summaries：run_bench `_summarize` 的输出列表。缺失键（None）跳过；
    全缺时该项为 None。标准差用总体口径（stp）——样本量小（2-5 次）时
    样本口径会系统性偏大，报告里注明口径即可。
    """
    keys = ("utility", "asr", "injection_task_utility")
    out: dict[str, dict] = {"runs": len(summaries)}
    for key in keys:
        values = [s[key] for s in summaries if s.get(key) is not None]
        if not values:
            out[key] = None
            out[f"{key}_std"] = None
            continue
        mean = statistics.fmean(values)
        std = statistics.pstdev(values) if len(values) > 1 else 0.0
        out[key] = round(mean, 4)
        out[f"{key}_std"] = round(std, 4)
    return out


def classify_case(row: dict) -> str:
    """单个注入用例的结果分类（analyze 报告的四列口径）。

    - injection_achieved：注入目标达成（真实 ASR +1，最坏结果）；
    - defended_task_done：注入在场但未达成，用户任务完成（防御成功的理想形态）；
    - defended_task_failed：注入未达成，但任务也没做成（拒绝/失败——区分
      "模型自拒"与"内核拦截"需要事件流采集，见遗留说明）；
    - no_injection：该用例没有注入任务，只看任务完成。
    """
    injection = row.get("injection_task")
    if not injection:
        return "no_injection"
    if row.get("attack_success"):
        return "injection_achieved"
    if row.get("utility"):
        return "defended_task_done"
    return "defended_task_failed"


def classify_counts(rows: list[dict]) -> dict[str, int]:
    """按分类计数（保持四键恒在，缺省 0，报告侧不需要判键）。"""
    counts = {
        "injection_achieved": 0,
        "defended_task_done": 0,
        "defended_task_failed": 0,
        "no_injection": 0,
    }
    for row in rows:
        counts[classify_case(row)] += 1
    return counts
