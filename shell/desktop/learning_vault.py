"""路由自学习状态的读写与面板摘要（不依赖 Qt）。

灵动岛是"每任务一个 Core"，任务结束 Core 即销毁；自校准路由选择器
（ContextualBanditSelector）因此在 live factory 的闭包里**跨任务共享**，
并在每次任务后落盘。本模块统一负责：

- :func:`load_selector` / :func:`save_selector`：共享选择器的容错读写；
- :func:`summarize`：把落盘的学习状态转成面板友好结构（空闲也能看，
  不依赖当前 Core）。

只用 yai_core 与标准库，CI 精简矩阵（无 Qt）也能按文件路径直接测试。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from yai_core import ContextualBanditSelector

#: 固定随机种子：演示可复现（探索随机性仍由 Beta 后验自然提供）。
LEARNING_SEED = 42

_LENGTH_LABELS = {
    "short": "短任务", "medium": "中等任务", "long": "长任务", "empty": "空任务"
}
_TOOLS_LABELS = {"none": "无工具", "few": "工具少", "many": "工具多"}
_STRATEGY_LABELS = {
    "direct": "直接回答", "react": "工具推理", "plan": "先规划", "clarify": "先澄清",
}


def describe_context(parts: list) -> str:
    """把 9 维上下文桶 key 渲染成简短中文标签。"""
    length, action, multistep, vague, question, has_latin, data_obj, unspec, tools = parts
    if multistep:
        kind = "多步任务"
    elif vague or unspec:
        kind = "需澄清"
    elif question:
        kind = "疑问查询"
    elif action:
        kind = "单步行动"
    else:
        kind = "一般对话"
    extras = []
    if data_obj:
        extras.append("含数据对象")
    if has_latin:
        extras.append("含英文")
    tail = "·".join(extras)
    return "·".join(
        x for x in (_LENGTH_LABELS.get(length, str(length)), kind,
                    _TOOLS_LABELS.get(tools, str(tools)), tail) if x
    )


def load_selector(path: str | Path) -> ContextualBanditSelector:
    """加载共享路由选择器；文件缺失/损坏时冷启动（纯规则先验），不影响启动。"""
    p = Path(path)
    try:
        if p.exists():
            return ContextualBanditSelector.load(p, seed=LEARNING_SEED)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass  # 状态不可读就从规则先验重新开始
    return ContextualBanditSelector(seed=LEARNING_SEED)


def save_selector(selector: ContextualBanditSelector, path: str | Path) -> bool:
    """落盘学习状态（自动建父目录）；失败不致命（演示可继续）。"""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        selector.save(p)
        return True
    except OSError:
        return False


def _summarize_dict(data: dict[str, Any]) -> dict[str, Any]:
    state = data.get("state", {})
    obs = data.get("obs", {})
    buckets = []
    total_obs = 0
    for key, table in state.items():
        observed = int(obs.get(key, 0))
        total_obs += observed
        means = {
            arm: (a / (a + b) if (a + b) > 0 else 0.0)
            for arm, (a, b) in table.items()
        }
        preferred = max(means, key=lambda arm: means[arm])
        buckets.append({
            "context": describe_context(json.loads(key)),
            "observed": observed,
            "preferred": preferred,
            "preferred_label": _STRATEGY_LABELS.get(preferred, preferred),
            "confidence": round(means[preferred], 3),
            "means": {
                arm: {"label": _STRATEGY_LABELS.get(arm, arm), "mean": round(m, 3)}
                for arm, m in means.items()
            },
        })
    buckets.sort(key=lambda b: (-b["observed"], b["context"]))
    return {
        "enabled": True,
        "selector": "ContextualBanditSelector",
        "learned_tasks": total_obs,
        "context_buckets": len(state),
        "buckets": buckets,
    }


def summarize(path: str | Path) -> dict[str, Any]:
    """读落盘学习状态并摘要；文件缺失/损坏返回 enabled=False 空结构。"""
    p = Path(path)
    try:
        if p.exists():
            return _summarize_dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    return {
        "enabled": False, "learned_tasks": 0, "context_buckets": 0, "buckets": []
    }
