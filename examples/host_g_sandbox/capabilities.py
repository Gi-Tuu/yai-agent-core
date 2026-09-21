"""host_g 演示宿主：一个极简的"候选人管理"应用，只暴露只读能力。

它刻意**没有**加权评分工具——这正是代码工具要补的能力缺口：
模型在运行中用 ``create_code_tool`` 生成一个加权评分函数，由宿主沙箱
（``sandbox.py``）在隔离子进程里执行。这样宿主无需为每种一次性算法
提前写好工具，Core 也能当场扩展能力。

运行：.venv/Scripts/python.exe examples/host_g_sandbox/run.py
"""

from __future__ import annotations

#: 演示数据：技能分（skill）与经验分（experience），满分 100。
SAMPLE_CANDIDATES: list[dict] = [
    {"name": "林晓", "skill": 88, "experience": 72},
    {"name": "陈默", "skill": 70, "experience": 95},
    {"name": "周岚", "skill": 95, "experience": 60},
]


def list_candidates() -> list[dict]:
    """列出全部候选人及其技能、经验分值（只读，返回副本）。"""
    return [dict(c) for c in SAMPLE_CANDIDATES]
