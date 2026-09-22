"""文本嵌入契约：为工具发现提供语义向量（第八个 SPI）。

模型（model）/通道（channel）/记忆（memory）/权限（policy）/发现（discovery）
/沙箱（sandbox）/学习（learning）之后，embedding 是第八个 SPI。

它独立于 ``ModelProvider``：聊天模型不一定提供 embedding，把嵌入能力拆成单独
契约，宿主可以只接一个 embedding 后端、不接聊天模型，反之亦然。

内核只定义契约、不内置任何实现，也不依赖 numpy：

- 零依赖的语义融合器（``yai_core.discovery.SemanticCatalog``）只用标准库
  ``math`` 算余弦；
- 真实后端（OpenAI 兼容 /embeddings，如 Agnes/通义/OpenAI）放在可选 extra，
  懒加载，不进内核硬依赖；
- 离线测试用确定性的 Fake embedder（概念轴向量），不访问网络、不需要 API Key。

不配置 embedder 时，工具发现退化为纯词法通道，行为与静态目录一致——语义是
增强，不是必需。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """把一批文本编码为等长向量。实现通常是 OpenAI 兼容的 /embeddings 客户端。"""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """返回与 ``texts`` 等长、等维度的向量列表。

        约定：
        - 保持顺序：``result[i]`` 对应 ``texts[i]``；
        - 同一实现内维度恒定，便于缓存候选向量后复用；
        - 失败时抛异常，由调用方决定降级（语义发现会退化为纯词法，不致命）。
        """
        ...
