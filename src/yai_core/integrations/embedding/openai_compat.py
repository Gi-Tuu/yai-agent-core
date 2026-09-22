"""OpenAI 兼容文本嵌入后端：把 /embeddings 接入 EmbeddingProvider 契约（可选 extra）。

- 懒加载 ``openai`` 包（复用 ``[llm]`` 可选依赖），内核本体仍零第三方硬依赖；
- 任何 OpenAI 兼容的 ``/embeddings`` 端点都可用：OpenAI、通义千问、智谱、本地
  vLLM / TEI 等；具体免费档（如 Agnes）是否提供 embedding 端点以其官方文档为准；
- 只负责"文本 → 向量"，供 ``yai_core.discovery.SemanticCatalog`` 注入；
- 不做任何离线单测之外的真实网络调用，真机验证见示例脚本。
"""

from __future__ import annotations

import os
from typing import Any


class OpenAICompatEmbedder:
    """``EmbeddingProvider`` 的 OpenAI 兼容实现（实例化需安装 ``.[llm]``）。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        dimensions: int | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "使用 OpenAICompatEmbedder 需要安装可选依赖：uv pip install -e '.[llm]'"
            ) from exc

        # 与 OpenAICompatProvider 共用 OPENAI_API_KEY / OPENAI_BASE_URL；
        # embedding 模型名单独用 EMBEDDING_MODEL，缺省回退 OpenAI 通用小模型。
        client_kwargs: dict[str, Any] = {
            "api_key": api_key or os.getenv("OPENAI_API_KEY", "missing"),
            "base_url": base_url or os.getenv("OPENAI_BASE_URL"),
        }
        if max_retries is not None:
            client_kwargs["max_retries"] = max_retries
        if timeout is not None:
            client_kwargs["timeout"] = timeout
        self.client = AsyncOpenAI(**client_kwargs)
        self.model = model or os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        # dimensions 仅 OpenAI text-embedding-3 等新模型支持，部分兼容端点会拒绝，
        # 因此默认不传，由调用方按所用模型显式开启。
        self.dimensions = dimensions

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        kwargs: dict[str, Any] = {"model": self.model, "input": texts}
        if self.dimensions is not None:
            kwargs["dimensions"] = self.dimensions
        resp = await self.client.embeddings.create(**kwargs)
        # 契约要求 result[i] 对应 texts[i]；显式按 index 排序，不依赖端点默认顺序。
        ordered = sorted(resp.data, key=lambda d: d.index)
        return [list(item.embedding) for item in ordered]
