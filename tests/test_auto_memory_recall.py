"""任务起始"主动召回"端到端测试（ScriptedModel，离线，无 API Key）。

验证开启 ``auto_memory_recall`` 后，内核在任务开始、进入规划/执行之前，会先用任务
文本检索长期记忆，把命中条目作为一条 system 上下文注入并发 ``memories_recalled``
事件——模型无需"先想到调 recall"就能用上用户画像；关闭开关时行为与原来完全一致。
"""

import asyncio
from typing import Any

from yai_core import AgentCore, ModelResponse, ScriptedModel
from yai_core.types import EventType

# 与 test_memory_vault.py 同款的记忆概念轴（中英双语触发词）。
_MEMORY_AXES = (
    ("mango", ("芒果", "过敏", "mango", "allergy")),
    ("dark", ("深色", "暗色", "dark", "夜间模式")),
)


class _MemoryFakeEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            norm = text.lower()
            vectors.append(
                [1.0 if any(word in norm for word in words) else 0.0 for _, words in _MEMORY_AXES]
            )
        return vectors


class RecordingModel(ScriptedModel):
    """记录每次模型调用实际收到的 messages；始终直接给最终答复（不调工具）。"""

    def __init__(self) -> None:
        super().__init__([ModelResponse(content="好的，我已结合你的长期记忆作答。")])
        self.batches: list[list[dict[str, Any]]] = []

    async def achat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tier: str = "standard",
    ):
        self.batches.append([dict(m) for m in messages])
        return await super().achat(messages, tools=tools, tier=tier)


def _has_injected_memory(model: RecordingModel, needle: str) -> bool:
    """模型收到的任一批消息里，是否存在含长期记忆标记与关键词的 system 消息。"""
    for batch in model.batches:
        for message in batch:
            if message.get("role") != "system":
                continue
            content = str(message.get("content", ""))
            if "长期记忆" in content and needle in content:
                return True
    return False


def test_auto_recall_injects_relevant_memory_at_task_start() -> None:
    model = RecordingModel()
    core = AgentCore(model, long_memory=True, auto_memory_recall=True, auto_approve_tools=True)
    # 直接写入一条长期记忆（不经模型），任务文本词法命中"芒果"。
    core.memory_vault.remember("用户对芒果严重过敏", kind="fact", tags=["过敏"], importance=5)

    result = asyncio.run(core.run("提醒我一下芒果相关的注意事项"))

    recalled = [e for e in result.events if e.type == EventType.MEMORIES_RECALLED]
    assert len(recalled) == 1
    assert recalled[0].data["count"] >= 1
    assert any("芒果" in item["content"] for item in recalled[0].data["items"])
    # 注入的 system 记忆确实到达了模型。
    assert _has_injected_memory(model, "芒果")


def test_auto_recall_off_by_default_no_injection() -> None:
    model = RecordingModel()
    core = AgentCore(model, long_memory=True, auto_approve_tools=True)
    core.memory_vault.remember("用户对芒果严重过敏", kind="fact", tags=["过敏"], importance=5)

    result = asyncio.run(core.run("提醒我一下芒果相关的注意事项"))

    assert not [e for e in result.events if e.type == EventType.MEMORIES_RECALLED]
    assert not _has_injected_memory(model, "芒果")


def test_auto_recall_uses_semantic_channel_with_embedder() -> None:
    # 跨语言、词法零重叠的任务，靠语义通道也能在任务起始把记忆捞出来注入。
    model = RecordingModel()
    core = AgentCore(
        model,
        long_memory=True,
        auto_memory_recall=True,
        memory_embedder=_MemoryFakeEmbedder(),
        auto_approve_tools=True,
    )
    core.memory_vault.remember("用户对芒果严重过敏", kind="fact", tags=["过敏"], importance=5)

    result = asyncio.run(core.run("mango allergy?"))

    recalled = [e for e in result.events if e.type == EventType.MEMORIES_RECALLED]
    assert recalled and recalled[0].data["count"] >= 1
    assert _has_injected_memory(model, "芒果")
