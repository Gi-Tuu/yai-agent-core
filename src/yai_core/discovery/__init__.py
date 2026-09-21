"""能力自发现：把宿主已有的函数/对象自动转成 ToolSpec，并按需发现新工具。"""

from yai_core.discovery.catalog import DiscoveredCandidate, StaticCatalog
from yai_core.discovery.introspect import build_spec, discover

__all__ = [
    "build_spec",
    "discover",
    "DiscoveredCandidate",
    "StaticCatalog",
]
