"""Anthropic Messages API 模型后端（Claude 系列）。

与 :class:`yai_core.llm.openai_compat.OpenAICompatProvider` 同形：宿主照样只实现
:meth:`achat`，内核拿到的仍是 :class:`ModelResponse`。差别全在这个适配器里翻译掉：

- **system 不在 messages 里**：Anthropic 把系统提示作为顶层 ``system`` 参数；
- **``max_tokens`` 必填**：OpenAI 侧可选，这里给默认值并可由宿主覆盖；
- **工具是内容块不是字段**：assistant 的 ``tool_calls`` 变成 ``tool_use`` 块，
  工具结果变成 **user 消息里的** ``tool_result`` 块（内核内部是 OpenAI 线格式，
  由本模块翻译，不要求宿主改上下文）；
- **工具描述形状不同**：内核给的是 ``{"type":"function","function":{...}}``，
  Anthropic 要 ``{"name","description","input_schema"}``。

依赖只进可选 extra（``anthropic``），内核 ``dependencies`` 保持为空；SDK 在构造函数里
懒加载，测试可注入假客户端，全程离线。
"""

from __future__ import annotations

import json
import os
from typing import Any

from yai_core.types import ModelResponse, TokenUsage, ToolCallRequest

_DEFAULT_MAX_TOKENS = 4096


def _as_text(content: Any) -> str:
    """把单条消息的 content 折成纯文本（静态 parts 尚未接入多模态）。

    既有契约：只有带 ``text`` 的 part 会被折进文本，其余 part（图片、文档等）
    **被跳过**——内核的消息契约当前是纯文本（``ChatMessage.content: str``），
    多模态 part 尚无落地路径。
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return str(content)



def _content_blocks(content: Any) -> list[dict[str, Any]]:
    """把一条消息的 content 转成 Anthropic 内容块（支持图片）。

    内核内部的消息形态是 **OpenAI 兼容**的内容数组（``ChatMessage.to_llm_dict()``
    的产物）：``{"type":"text"}`` / ``{"type":"image_url","image_url":{"url":"data:..."}}``。
    这里把 ``image_url`` 的 data URI 拆成 Claude 的
    ``{"type":"image","source":{"type":"base64",...}}``——**图片因此真的能被模型看到**，
    而不是被压平成一句空文本。

    认不出的块一律降级成文字块（保留它携带的 text），不静默丢。
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            text = str(part.get("text", ""))
            if text:
                blocks.append({"type": "text", "text": text})
            continue
        if kind == "image_url":
            image = part.get("image_url")
            url = str(image.get("url", "")) if isinstance(image, dict) else ""
            if url.startswith("data:") and ";base64," in url:
                header, payload = url.split(";base64,", 1)
                media_type = header[len("data:") :] or "image/png"
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": payload,
                        },
                    }
                )
            else:
                # 非内联引用：内核不替宿主抓取外部 URL，降级成明说"看不到"的文字块。
                blocks.append(
                    {
                        "type": "text",
                        "text": f"[图片引用未内联：{url or '（无引用）'} —— 模型看不到这张图]",
                    }
                )
            continue
        text = str(part.get("text", ""))
        blocks.append(
            {"type": "text", "text": text} if text else {"type": "text", "text": "[未知片段]"}
        )
    return blocks


def split_system(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """抽出 system 消息，返回（系统提示文本, 其余消息）。多条 system 合并成一段。"""
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "system":
            text = _as_text(message.get("content")).strip()
            if text:
                system_parts.append(text)
        else:
            rest.append(message)
    return ("\n\n".join(system_parts) or None), rest


def _tool_use_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """assistant 的 tool_calls → tool_use 内容块；参数解析失败按空对象处理。"""
    blocks: list[dict[str, Any]] = []
    text = _as_text(message.get("content")).strip()
    if text:
        blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_args = function.get("arguments")
        if isinstance(raw_args, str):
            try:
                arguments = json.loads(raw_args or "{}")
            except json.JSONDecodeError:
                arguments = {}
        elif isinstance(raw_args, dict):
            arguments = raw_args
        else:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or f"toolu_{len(blocks)}"),
                "name": str(function.get("name") or ""),
                "input": arguments,
            }
        )
    return blocks


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI 线格式 → Anthropic messages：翻译角色与内容块，并合并相邻同角色。

    两条硬性形状要求：工具结果必须挂在 **user** 消息上；同一角色连续出现要合成一条
    （否则一次调用里多个工具结果会变成多条 user 消息）。
    """
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        if role == "assistant":
            blocks = _tool_use_blocks(message)
        elif role == "tool":
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id") or ""),
                    "content": _as_text(message.get("content")),
                }
            ]
            role = "user"
        else:
            blocks = _content_blocks(message.get("content"))
        if not blocks:
            continue
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})
    return converted


def to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """内核的 function-calling 描述 → Anthropic tools（name/description/input_schema）。"""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        inner = tool.get("function")
        function: dict[str, Any] = inner if isinstance(inner, dict) else tool
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        schema = function.get("parameters")
        entry: dict[str, Any] = {
            "name": name,
            "input_schema": (
                schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
            ),
        }
        description = str(function.get("description") or "").strip()
        if description:
            entry["description"] = description
        converted.append(entry)
    return converted


def parse_response(response: Any) -> ModelResponse:
    """Anthropic 响应 → ModelResponse；非文本/非 tool_use 的块（如思考块）忽略但保留 raw。"""
    texts: list[str] = []
    tool_calls: list[ToolCallRequest] = []
    for block in getattr(response, "content", None) or []:
        block_type = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if block_type == "tool_use":
            raw_input = getattr(block, "input", None)
            if raw_input is None and isinstance(block, dict):
                raw_input = block.get("input")
            if not isinstance(raw_input, dict):
                raise ValueError("工具调用参数必须是合法 JSON 对象")
            arguments = raw_input
            name = getattr(block, "name", None) or (
                block.get("name") if isinstance(block, dict) else ""
            )
            call_id = getattr(block, "id", None) or (
                block.get("id") if isinstance(block, dict) else ""
            )
            tool_calls.append(
                ToolCallRequest(id=str(call_id or ""), name=str(name or ""), arguments=arguments)
            )
        elif block_type == "text":
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text:
                texts.append(str(text))
    usage: TokenUsage | None = None
    raw_usage = getattr(response, "usage", None)
    if raw_usage is not None:
        read = (
            (lambda key: getattr(raw_usage, key, 0))
            if not isinstance(raw_usage, dict)
            else (lambda key: raw_usage.get(key, 0))
        )
        prompt = int(read("input_tokens") or 0)
        completion = int(read("output_tokens") or 0)
        usage = TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        )
    return ModelResponse(
        content="\n".join(texts), tool_calls=tool_calls, raw=response, usage=usage
    )


class AnthropicProvider:
    """Claude Messages API 后端；宿主只需给 API Key 与模型名。"""

    # 真实联网模型后端：供 llm_router="auto" 识别（离线脚本模型不声明此标记）。
    yai_live_router = True
    #: 本后端能吃多模态（Claude 的 text / image 块）。
    yai_multimodal = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        strong_model: str | None = None,
        max_tokens: int | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
        client: Any = None,
        prompt_cache: bool = True,
    ) -> None:
        if client is None:
            try:
                from anthropic import AsyncAnthropic  # 懒加载：只有真用它才需要 SDK
            except ImportError as exc:
                raise ImportError(
                    "使用 AnthropicProvider 需要安装可选依赖："
                    "uv sync --extra anthropic（或 pip install 'yai-agent-core[anthropic]'）"
                ) from exc
            kwargs: dict[str, Any] = {
                "api_key": api_key or os.getenv("ANTHROPIC_API_KEY", "missing")
            }
            url = base_url or os.getenv("ANTHROPIC_BASE_URL")
            if url:
                kwargs["base_url"] = url
            # 交给 FallbackModelProvider 编排时可传 max_retries=0 让外层快速降级，
            # 避免 SDK 内部重试与兜底重复等待；默认 None 保留 SDK 行为。
            if max_retries is not None:
                kwargs["max_retries"] = max_retries
            if timeout is not None:
                kwargs["timeout"] = timeout
            client = AsyncAnthropic(**kwargs)
        self.client = client
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self.strong_model = strong_model or os.getenv("ANTHROPIC_STRONG_MODEL") or self.model
        # max_tokens 在 Anthropic 是必填：显式参数 > 环境变量 > 默认值。
        configured_max: int | str | None = (
            max_tokens if max_tokens is not None else os.getenv("ANTHROPIC_MAX_TOKENS")
        )
        self.max_tokens = int(configured_max) if configured_max else _DEFAULT_MAX_TOKENS
        # 提示缓存：system 与工具清单是每轮都相同的前缀，打上 cache_control 后
        # 输入 token 按官方计价可省一个数量级（缓存命中时）。兼容代理若拒绝块状
        # system，传 False 退回纯字符串。
        self.prompt_cache = prompt_cache

    async def achat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tier: str = "standard",
    ) -> ModelResponse:
        system, rest = split_system(messages)
        kwargs: dict[str, Any] = {
            "model": self.strong_model if tier == "strong" else self.model,
            "messages": to_anthropic_messages(rest) or [
                {"role": "user", "content": [{"type": "text", "text": ""}]}
            ],
            "max_tokens": self.max_tokens,
        }
        if system:
            if self.prompt_cache:
                # 块状 system 才能挂 cache_control；断点打在 system 末尾。
                kwargs["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                kwargs["system"] = system
        converted_tools = to_anthropic_tools(tools) if tools else []
        if converted_tools:
            if self.prompt_cache:
                # 工具定义紧随 system，一起构成稳定前缀；断点只打最后一个工具。
                converted_tools[-1] = {
                    **converted_tools[-1],
                    "cache_control": {"type": "ephemeral"},
                }
            kwargs["tools"] = converted_tools
        response = await self.client.messages.create(**kwargs)
        return parse_response(response)
