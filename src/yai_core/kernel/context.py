from __future__ import annotations

from yai_core.types import (
    ChatMessage,
    ImagePart,
    ResourcePart,
    flatten_content_blocks,
)

#: 一张图片在上下文里的**估算成本**（字符口径）。图片的真实 token 成本远高于它
#: 渲染出来的占位文本长度——按占位长度计账，等于"图片不占预算"，长会话里会被
#: 悄悄塞满。这个值是**保守代理**（不是精确计费）：一张图约 1k–4k token 量级。
IMAGE_CONTEXT_COST_CHARS = 4000


def message_cost(
    message: ChatMessage, *, image_cost_chars: int = IMAGE_CONTEXT_COST_CHARS
) -> int:
    """一条消息在上下文预算里的成本（字符口径）。

    总量、硬裁剪扣减、摘要压缩选区必须共用这一个函数：只让总量计入 parts，
    裁剪与压缩却还按 ``content`` 计算，会让长片段"看起来不占预算"——图片被
    无谓多删、纯文本片段则可能永远触发不了摘要。
    """
    total = len(message.content)
    for part in getattr(message, "parts", None) or ():
        if isinstance(part, ImagePart):
            total += image_cost_chars
        elif isinstance(part, ResourcePart):
            total += len(part.uri) + len(part.name)
        else:
            total += len(getattr(part, "text", ""))
    return total


class Context:
    """单次运行的对话上下文。

    超预算有两层处理：
    - :meth:`enforce_budget`（硬裁剪，同步、零依赖、确定性）：直接丢弃最旧整轮，
      是最终安全网，``add`` 时自动触发，也供摘要压缩失败时回退；
    - 摘要压缩（见 ``kernel/compaction.py``，需模型、异步）：把旧轮压成一条摘要，
      保留要点，经 :meth:`replace_range` 替换。
    """

    def __init__(
        self,
        system_prompt: str,
        *,
        max_chars: int = 24000,
        image_cost_chars: int = IMAGE_CONTEXT_COST_CHARS,
    ) -> None:
        self.max_chars = max_chars
        self.image_cost_chars = image_cost_chars
        self.messages: list[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]

    def add(self, message: ChatMessage) -> None:
        self.messages.append(message)
        self.enforce_budget()

    def total_chars(self) -> int:
        """当前全部消息的字符总量（朴素口径，用于触发压缩/裁剪）。

        多模态片段按**真实成本口径**计入：图片按 :data:`IMAGE_CONTEXT_COST_CHARS`
        记（而不是它那段占位文本的长度——那会让"图片不占预算"），文本片段按实际
        长度，资源引用按其元数据长度（很小）。
        """
        return sum(
            message_cost(message, image_cost_chars=self.image_cost_chars)
            for message in self.messages
        )

    def replace_range(self, start: int, end: int, summary: ChatMessage) -> None:
        """用一条摘要消息替换半开区间 ``[start, end)``。

        system 消息（index 0）永留，因此 ``start`` 不得小于 1；``end`` 之后的
        消息（最近完整轮）原样保留。调用方必须保证区间对齐轮首、不切断
        assistant(tool_calls)/tool 配对。
        """
        if start < 1:
            raise ValueError("替换起点不能早于 system 消息（index 0）")
        if end <= start:
            raise ValueError("替换区间终点必须大于起点")
        self.messages = [*self.messages[:start], summary, *self.messages[end:]]

    def enforce_budget(self) -> None:
        """硬裁剪：超预算时丢弃最旧的整轮历史；system 永留，至少保留最后一轮。

        v0.3 修复：旧实现按下标逐个丢，可能从 assistant(tool_calls)/tool 配对中间
        切开，发给 OpenAI 兼容接口会直接 400。现改为把候补丢弃边界**向后对齐到
        轮首**（role == "user"），规则与 memory/retention.py 同源；为避免 kernel
        反向依赖 memory 包，这里保留一份私有实现，两处改动必须同步（第 13 章讲义）。
        候补边界之后没有新一轮时，宁可超预算也保留最后一轮（含其工具消息）。
        """
        total = self.total_chars()
        i = 1
        while total > self.max_chars and i < len(self.messages) - 1:
            total -= message_cost(
                self.messages[i], image_cost_chars=self.image_cost_chars
            )
            i += 1
        if i <= 1:
            return
        aligned = i
        while aligned < len(self.messages) and self.messages[aligned].role != "user":
            aligned += 1
        if aligned >= len(self.messages):
            # 候补边界之后没有新一轮：保留最后一轮，不做裁剪。
            return
        self.messages = [self.messages[0], *self.messages[aligned:]]

    def llm_messages(self, *, multimodal: bool = True) -> list[dict]:
        """产出 OpenAI 兼容的消息字典列表。

        ``multimodal=False`` 时（后端不支持内容数组）**把片段降级成纯文本**：
        文本块原样保留，图片与资源引用换成一���说明"模型看不到它"的占位——
        宁可让模型明知道自己缺资料，也不要发一个后端不认的数组。
        """
        out: list[dict] = []
        for message in self.messages:
            payload = message.to_llm_dict()
            if not multimodal and isinstance(payload.get("content"), list):
                payload["content"] = flatten_content_blocks(payload["content"])
            out.append(payload)
        return out
