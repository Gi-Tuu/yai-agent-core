"""OpenAI 兼容模型后端：DeepSeek / 通义千问 / OpenAI / 本地 vLLM 等同一套代码。"""

from __future__ import annotations

import json
import os
from typing import Any

from yai_core.types import ModelResponse, TokenUsage, ToolCallRequest


class OpenAICompatProvider:
    # 真实联网模型后端：供 llm_router="auto" 识别（离线脚本模型不声明此标记）。
    yai_live_router = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        strong_model: str | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "使用 OpenAICompatProvider 需要安装可选依赖：uv pip install -e '.[llm]'"
            ) from exc

        # 交给 FallbackModelProvider 编排时可传 max_retries=0 让其快速失败、尽快降级，
        # 避免 SDK 内部重试和外层兜底重复等待；默认 None 保留 SDK 行为。
        client_kwargs: dict[str, Any] = {
            "api_key": api_key or os.getenv("OPENAI_API_KEY", "missing"),
            "base_url": base_url or os.getenv("OPENAI_BASE_URL"),
        }
        if max_retries is not None:
            client_kwargs["max_retries"] = max_retries
        if timeout is not None:
            client_kwargs["timeout"] = timeout
        self.client = AsyncOpenAI(**client_kwargs)
        self.model = model or os.getenv("LLM_MODEL", "deepseek-chat")
        self.strong_model = strong_model or os.getenv("LLM_STRONG_MODEL") or self.model

    async def achat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tier: str = "standard",
    ) -> ModelResponse:
        # messages 由 Context.llm_messages() 产出，已经是 OpenAI 线格式 dict
        kwargs: dict[str, Any] = {
            "model": self.strong_model if tier == "strong" else self.model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        completion = await self.client.chat.completions.create(**kwargs)
        msg = completion.choices[0].message

        tool_calls: list[ToolCallRequest] = []
        for tc in getattr(msg, "tool_calls", None) or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCallRequest(id=tc.id, name=tc.function.name, arguments=args))

        # 透传真实 token 用量（任务级 token 预算据此累计）；后端不返回 usage 时留 None，
        # 内核不臆造数字，此时 token 上限不生效、改用次数上限兜底。
        usage: TokenUsage | None = None
        raw_usage = getattr(completion, "usage", None)
        if raw_usage is not None:
            prompt = int(getattr(raw_usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(raw_usage, "completion_tokens", 0) or 0)
            total = int(getattr(raw_usage, "total_tokens", 0) or 0) or (
                prompt + completion_tokens
            )
            usage = TokenUsage(
                prompt_tokens=prompt,
                completion_tokens=completion_tokens,
                total_tokens=total,
            )

        return ModelResponse(
            content=msg.content or "", tool_calls=tool_calls, raw=completion, usage=usage
        )
