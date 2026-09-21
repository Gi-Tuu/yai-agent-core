"""ToolRegistry 两层工具目录测试：轻量 catalog + 按需 schemas_for。"""

import pytest

from yai_core.tools.registry import ToolRegistry, _one_line
from yai_core.types import ToolSpec


def _spec(name: str, description: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {}},
        handler=lambda: None,
    )


def test_catalog_returns_name_and_one_line_summary() -> None:
    reg = ToolRegistry()
    reg.register(_spec("list_orders", "列出订单\n第二行细节不应进入摘要"))
    reg.register(_spec("sum_amount", "按区域和品类统计销售额"))

    cat = reg.catalog()
    assert cat == [
        {"name": "list_orders", "summary": "列出订单"},
        {"name": "sum_amount", "summary": "按区域和品类统计销售额"},
    ]
    # 目录比完整 schema 轻量：不含 parameters。
    assert all("parameters" not in item for item in cat)


def test_catalog_truncates_long_summary() -> None:
    long_desc = "描" * 100
    summary = _one_line(long_desc)
    assert len(summary) == 80
    assert summary.endswith("…")


def test_schemas_for_returns_full_schema_for_named_tools() -> None:
    reg = ToolRegistry()
    reg.register(_spec("a", "甲工具"))
    reg.register(_spec("b", "乙工具"))

    schemas = reg.schemas_for(["b"])
    assert len(schemas) == 1
    assert schemas[0]["type"] == "function"
    assert schemas[0]["function"]["name"] == "b"
    assert "parameters" in schemas[0]["function"]


def test_schemas_for_preserves_requested_order() -> None:
    reg = ToolRegistry()
    reg.register(_spec("a", "甲"))
    reg.register(_spec("b", "乙"))
    reg.register(_spec("c", "丙"))

    names = [s["function"]["name"] for s in reg.schemas_for(["c", "a"])]
    assert names == ["c", "a"]


def test_schemas_for_unknown_name_raises() -> None:
    reg = ToolRegistry()
    reg.register(_spec("a", "甲"))
    with pytest.raises(KeyError):
        reg.schemas_for(["a", "ghost"])
