"""Embedding 集成：把外部文本嵌入服务接入 SemanticCatalog（可选、懒加载）。"""

from yai_core.integrations.embedding.local_bge import LocalBgeEmbedder
from yai_core.integrations.embedding.openai_compat import OpenAICompatEmbedder

__all__ = ["LocalBgeEmbedder", "OpenAICompatEmbedder"]
