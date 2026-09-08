from __future__ import annotations

from yai_core.types import ChatMessage


class Context:
    """单次运行的对话上下文。v0.1 用朴素字符估算做 token 预算，v2 换 tiktoken。"""

    def __init__(self, system_prompt: str, *, max_chars: int = 24000) -> None:
        self.max_chars = max_chars
        self.messages: list[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]

    def add(self, message: ChatMessage) -> None:
        self.messages.append(message)
        self._compact()

    def _compact(self) -> None:
        """超出预算时，从最旧的非 system、非首轮消息开始丢弃。"""
        total = sum(len(m.content) for m in self.messages)
        i = 1
        while total > self.max_chars and i < len(self.messages) - 1:
            total -= len(self.messages[i].content)
            i += 1
        if i > 1:
            self.messages = [self.messages[0], *self.messages[i:]]

    def llm_messages(self) -> list[dict]:
        return [m.to_llm_dict() for m in self.messages]
