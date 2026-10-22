from __future__ import annotations

from yai_core.types import ChatMessage, parts_to_text


class Context:
    """单次运行的对话上下文。

    超预算有两层处理：
    - :meth:`enforce_budget`（硬裁剪，同步、零依赖、确定性）：直接丢弃最旧整轮，
      是最终安全网，``add`` 时自动触发，也供摘要压缩失败时回退；
    - 摘要压缩（见 ``kernel/compaction.py``，需模型、异步）：把旧轮压成一条摘要，
      保留要点，经 :meth:`replace_range` 替换。
    """

    def __init__(self, system_prompt: str, *, max_chars: int = 24000) -> None:
        self.max_chars = max_chars
        self.messages: list[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]

    def add(self, message: ChatMessage) -> None:
        self.messages.append(message)
        self.enforce_budget()

    def total_chars(self) -> int:
        """当前全部消息的字符总量（朴素口径，用于触发压缩/裁剪）。

        多模态片段按它们的**文本渲染长度**计入：图片与资源引用渲染成占位文本，
        长度很小——但"图片在上下文里"这件事本身不会让预算失控，所以这个口径是够用的。
        """
        total = sum(len(m.content) for m in self.messages)
        total += sum(
            len(parts_to_text(m.parts)) for m in self.messages if getattr(m, "parts", None)
        )
        return total

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
            total -= len(self.messages[i].content)
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

    def llm_messages(self) -> list[dict]:
        return [m.to_llm_dict() for m in self.messages]
