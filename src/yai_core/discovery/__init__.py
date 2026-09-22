"""能力自发现：把宿主已有的函数/对象自动转成 ToolSpec，并按需发现新工具。"""

from yai_core.discovery.catalog import DiscoveredCandidate, StaticCatalog
from yai_core.discovery.introspect import build_spec, discover
from yai_core.discovery.scoring import (
    DEFAULT_SYNONYM_GROUPS,
    LexicalHit,
    canonicalize,
    lexical_score,
    normalize,
    token_similarity,
    tokenize,
)
from yai_core.discovery.semantic import SemanticCatalog, cosine_similarity

__all__ = [
    "build_spec",
    "canonicalize",
    "cosine_similarity",
    "discover",
    "DiscoveredCandidate",
    "DEFAULT_SYNONYM_GROUPS",
    "LexicalHit",
    "lexical_score",
    "normalize",
    "SemanticCatalog",
    "StaticCatalog",
    "token_similarity",
    "tokenize",
]
