"""端到端离线评测：量化 Agent 的完成率、工具正确性、越权与无支撑断言。

这个包是**开发 / CI 工具**，不参与运行时 Agent 行为，且零第三方硬依赖。
典型用法::

    from yai_core.evaluation import GoldenTask, collect_trace, score_task, aggregate

    events = collect_events_from_astream()      # 跑 AgentCore.astream 收集事件
    trace = collect_trace(events)               # 还原结构化轨迹
    score = score_task(golden, trace)           # 对照标注任务打分
    summary = aggregate([(golden, trace, score)])  # 汇总整批指标

脚本模型（离线）或真实模型都能跑同一套 golden 任务：前者作为确定性回归基线，
后者用来量化真实模型在工具调用、多步完成上的表现。完整 runner 见
``scripts/eval_agent.py``。
"""

from yai_core.evaluation.scoring import (
    EvalResult,
    EvalSummary,
    TaskScore,
    Trace,
    aggregate,
    collect_trace,
    score_task,
)
from yai_core.evaluation.tasks import GoldenTask

__all__ = [
    "EvalResult",
    "EvalSummary",
    "GoldenTask",
    "TaskScore",
    "Trace",
    "aggregate",
    "collect_trace",
    "score_task",
]
