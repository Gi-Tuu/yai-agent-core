"""宿主 F：演示"按需发现能力"的最小宿主（一个待办工作台）。

这里刻意把能力分成两层：

- **默认能力**：``list_tasks`` / ``add_task``——工作台的核心功能，进程启动即注册；
- **可选能力**：``get_weather``——不默认注册，只登记进 StaticCatalog。
  只有当 Agent 感知到"能力缺口"、且目录里恰好有匹配候选时，它才会被
  发现并注册（执行时仍走统一的权限策略，"能被发现"不等于"被授权执行"）。

注意：本宿主不用 ``AgentCore.auto`` 内省整个模块——否则 ``get_weather``
会在启动时就被注册，演示不出"按需长出能力"。run.py 只显式注册默认能力，
把可选能力放进目录。
"""

from __future__ import annotations

_TASKS: list[dict] = [
    {"id": 1, "title": "提交上海开源赛材料", "done": False},
    {"id": 2, "title": "录制段7 开源治理", "done": False},
]


# ---------- 默认能力（启动即注册） ----------

def list_tasks() -> list[dict]:
    """列出全部待办事项。"""
    return _TASKS


def add_task(title: str) -> dict:
    """新增一条待办事项。"""
    task = {"id": len(_TASKS) + 1, "title": title, "done": False}
    _TASKS.append(task)
    return task


# ---------- 可选能力（默认不注册，只在按需目录里待命） ----------

def get_weather(city: str) -> dict:
    """查询指定城市的实时天气。"""
    # 演示数据：真实宿主可在此调用天气 API。
    # 关键点：目录里没有的能力永远不会被发现——目录本身就是一层授权。
    return {"city": city, "weather": "晴", "temp_c": 27}
