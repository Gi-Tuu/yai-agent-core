"""语言中立能力清单（manifest）测试：ToolSpec 序列化、跨实例移植、文件 IO。

全程离线、不依赖 API Key；重点验证 composite 工作流"数据即能力"——
导出后在一个全新注册表 / 执行器里仍能端到端跑通。
"""

import asyncio
import json

import pytest

from yai_core.channels import CollectChannel
from yai_core.discovery.introspect import build_spec
from yai_core.policy import AllowlistPolicy
from yai_core.tools import (
    MANIFEST_VERSION,
    ToolExecutor,
    ToolRegistry,
    build_composite_spec,
    read_manifest,
    specs_from_manifest,
    specs_to_manifest,
    write_manifest,
)
from yai_core.types import ToolSpec


# —— 测试用的底层能力 ——
def greet(name: str) -> str:
    """打招呼。"""
    return f"hi {name}"


def add_one(value: int) -> int:
    """加一。"""
    return value + 1


def double(value: int) -> int:
    """翻倍。"""
    return value * 2


def test_native_manifest_roundtrip_drops_handler():
    spec = build_spec(greet)
    manifest = spec.to_manifest_dict()

    assert "handler" not in manifest  # 运行时对象不可序列化
    assert manifest["name"] == "greet"
    assert manifest["source"] == "native"

    restored = ToolSpec.from_manifest_dict(manifest)
    assert restored.handler is None  # 导入后需绑定后端
    assert restored.name == "greet"
    assert restored.description == spec.description
    assert restored.input_schema == spec.input_schema


def test_code_manifest_roundtrip_keeps_code():
    spec = ToolSpec(
        name="calc",
        description="计算",
        input_schema={"type": "object", "properties": {}},
        handler=None,
        source="code",
        code="def run(inputs):\n    return 1",
    )
    manifest = spec.to_manifest_dict()
    assert manifest["code"].startswith("def run")

    restored = ToolSpec.from_manifest_dict(manifest)
    assert restored.source == "code"
    assert restored.code == spec.code
    assert restored.handler is None


def test_composite_manifest_runs_in_a_fresh_core():
    # 原实例：构造一个编排 add_one -> double 的组合工具
    composite = build_composite_spec(
        "chain",
        "先加一再翻倍",
        [
            {"tool": "add_one", "args": {"value": "{{$input.value}}"}},
            {"tool": "double", "args": {"value": "{{$steps.0}}"}},
        ],
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
    )

    # 跨"实例"：只通过 manifest JSON 传递（handler 不被携带）
    moved = ToolSpec.from_manifest_dict(composite.to_manifest_dict())
    assert moved.handler is None
    assert [s.tool for s in moved.steps] == ["add_one", "double"]

    # 全新注册表：底层能力 + 移植来的组合工具
    registry = ToolRegistry()
    registry.register_many([build_spec(add_one), build_spec(double), moved])

    executor = ToolExecutor(
        registry, AllowlistPolicy(mode="allow_all"), CollectChannel()
    )
    events, ok, text = asyncio.run(executor.execute("chain", {"value": 5}))

    assert ok is True
    assert json.loads(text) == {"steps": [6, 12]}
    assert events[0].type == "tool_composed"


def test_bulk_manifest_roundtrip():
    specs = [build_spec(greet), build_spec(add_one)]
    manifest = specs_to_manifest(specs)

    assert manifest["manifest_version"] == MANIFEST_VERSION
    assert len(manifest["tools"]) == 2

    restored = specs_from_manifest(manifest)
    assert [s.name for s in restored] == ["greet", "add_one"]
    assert all(s.handler is None for s in restored)


def test_manifest_file_roundtrip(tmp_path):
    target = tmp_path / "nested" / "manifest.json"
    write_manifest(target, [build_spec(greet)])

    assert target.exists()
    restored = read_manifest(target)
    assert len(restored) == 1 and restored[0].name == "greet"


def test_manifest_error_cases():
    # 工具缺 name
    with pytest.raises(ValueError):
        ToolSpec.from_manifest_dict({"description": "x"})
    # source 非法
    with pytest.raises(ValueError):
        ToolSpec.from_manifest_dict({"name": "x", "source": "bogus"})
    # composite 缺 steps
    with pytest.raises(ValueError):
        ToolSpec.from_manifest_dict({"name": "c", "source": "composite"})
    # composite step 缺 tool
    with pytest.raises(ValueError):
        ToolSpec.from_manifest_dict(
            {"name": "c", "source": "composite", "steps": [{"args": {}}]}
        )
    # 版本不认识
    with pytest.raises(ValueError):
        specs_from_manifest({"manifest_version": 99, "tools": []})
    # 缺 tools 数组
    with pytest.raises(ValueError):
        specs_from_manifest({"manifest_version": MANIFEST_VERSION})
    # 类型错误
    with pytest.raises(TypeError):
        ToolSpec.from_manifest_dict("nope")
    with pytest.raises(TypeError):
        specs_from_manifest([])
