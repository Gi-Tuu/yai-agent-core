"""语言中立能力清单（manifest）测试：ToolSpec 序列化、跨实例移植、文件 IO。

全程离线、不依赖 API Key；重点验证 composite 工作流"数据即能力"——
导出后在一个全新注册表 / 执行器里仍能端到端跑通。
另一半重点：治理声明（脱敏路径 / 宽口径审计 / 契约快照）随清单走，
而 ``gate`` 作为函数既不导出也不重建——外来清单不能替本机工具施加准入。
"""

import asyncio
import json
from dataclasses import replace

import pytest

from yai_core.channels import CollectChannel
from yai_core.discovery.introspect import build_spec
from yai_core.integrations.acc import build_gate, parse_declaration
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
from yai_core.types import Subject, ToolGovernance, ToolSpec


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


def refund(order_id: str, amount: int) -> str:
    """退款。"""
    return f"{order_id}:{amount}"


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


# ---------- 治理声明随清单走（gate 永不随清单走） ----------

#: 一份合法的 ACC v1 声明：要求可信主体 + 大额退款要人工确认 + 审计收口。
ACC_DECL = {
    "version": 1,
    "enabled": True,
    "scope": "order.write",
    "risk": {"level": "high"},
    "subject": {"required": True},
    "approval": {
        "required": False,
        "prompt": "确认向 {order_id} 退款 {amount} 元？",
        "when": [{"param": "amount", "op": ">", "value": 100, "label": "大额"}],
    },
    "audit": {"sensitive": True},
}


def _moved(spec: ToolSpec) -> ToolSpec:
    """只经过一次 JSON 往返（模拟跨进程 / 跨语言搬运）。"""
    return ToolSpec.from_manifest_dict(json.loads(json.dumps(spec.to_manifest_dict())))


def _bound(spec: ToolSpec) -> ToolSpec:
    """搬运后绑定执行后端——manifest 本来就不带 handler，宿主侧必须接上。"""
    spec.handler = refund
    return spec


def test_governance_travels_with_manifest():
    spec = build_spec(refund)
    spec.governance = ToolGovernance(
        sensitive=("order_id",), audit_sensitive=True, acc=dict(ACC_DECL)
    )
    manifest = spec.to_manifest_dict()

    assert manifest["governance"] == {
        "sensitive": ["order_id"],
        "audit_sensitive": True,
        "acc": ACC_DECL,
    }
    assert "gate" not in manifest["governance"]  # 函数不导出

    moved = _moved(spec)
    assert moved.governance.sensitive == ("order_id",)
    assert moved.governance.audit_sensitive is True
    assert moved.governance.acc == ACC_DECL      # 声明快照逐字段无损
    assert moved.governance.gate is None
    assert moved.to_manifest_dict() == manifest  # 再导出幂等


def test_declared_paths_still_redact_after_transport():
    """脱敏路径不是本机专属装饰：清单搬过去后，声明仍作用于对外副本。"""
    spec = build_spec(refund)
    spec.governance = ToolGovernance(sensitive=("order_id",))
    moved = _moved(spec)
    _bound(moved)

    registry = ToolRegistry()
    registry.register(moved)
    events, ok, _ = asyncio.run(
        ToolExecutor(
            registry, AllowlistPolicy((), mode="auto"), CollectChannel()
        ).execute("refund", {"order_id": "ORD-SECRET-1", "amount": 10})
    )
    assert ok is True
    call = next(e for e in events if e.type == "tool_call")
    assert "ORD-SECRET-1" not in str(call.data)
    assert call.data["arguments"]["order_id"].startswith("[redacted")


def test_ungoverned_manifest_stays_minimal():
    manifest = build_spec(greet).to_manifest_dict()
    assert "governance" not in manifest  # 无声明就不造噪声字段（旧格式逐字节不变）
    assert ToolSpec.from_manifest_dict(manifest).governance == ToolGovernance()


def test_foreign_acc_snapshot_never_builds_a_gate():
    """外来清单只"说"这个能力要求主体；内核不据此加闸门，也不因此放行。"""
    spec = build_spec(refund)
    spec.governance = ToolGovernance(acc=dict(ACC_DECL))
    moved = _bound(_moved(spec))

    registry = ToolRegistry()
    registry.register(moved)
    events, ok, text = asyncio.run(
        ToolExecutor(
            registry, AllowlistPolicy(mode="allow_all"), CollectChannel()
        ).execute("refund", {"order_id": "A-1", "amount": 500})
    )
    assert ok is True, text  # 没重新绑定 gate = 清单里的声明是惰性的


def test_host_rebinding_restores_the_gate():
    """宿主本地重新绑定后，搬运过的声明与原件行为一致。"""
    spec = build_spec(refund)
    spec.governance = ToolGovernance(acc=dict(ACC_DECL))
    moved = _bound(_moved(spec))

    parsed = parse_declaration(moved.governance.acc)
    assert parsed.valid and parsed.declaration is not None
    moved.governance = replace(
        moved.governance, gate=build_gate(parsed.declaration, moved.input_schema)
    )

    registry = ToolRegistry()
    registry.register(moved)
    executor = ToolExecutor(
        registry, AllowlistPolicy(mode="allow_all"), CollectChannel()
    )
    # 无主体 → 声明的 subject.required 生效，连问人都不必问
    _, ok, text = asyncio.run(
        executor.execute("refund", {"order_id": "A-1", "amount": 10})
    )
    assert ok is False
    assert "可信操作主体" in text
    # 有主体 → 放行；再叠加大额条件 → 要人工确认
    events, ok, _ = asyncio.run(
        executor.execute(
            "refund", {"order_id": "A-1", "amount": 500}, subject=Subject("u-1")
        )
    )
    assert ok is True
    assert any(e.type == "permission_asked" for e in events)


def test_governance_file_roundtrip(tmp_path):
    spec = build_spec(greet)
    spec.governance = ToolGovernance(sensitive=("name",), audit_sensitive=True)
    target = tmp_path / "gov.json"
    write_manifest(target, [spec])

    moved = read_manifest(target)[0]
    assert moved.governance.sensitive == ("name",)
    assert moved.governance.audit_sensitive is True


def test_governance_validation_rejects_dirty_data():
    def load(gov):
        return ToolSpec.from_manifest_dict({"name": "t", "governance": gov})

    with pytest.raises(ValueError, match="必须是 object"):
        load(["api_key"])
    with pytest.raises(ValueError, match="不认识"):
        load({"sensitives": ["api_key"]})           # 写错的键不能被静默吞掉
    with pytest.raises(ValueError, match="不认识"):
        load({"gate": True})                       # 想 smuggle 准入逻辑 = 拒绝
    with pytest.raises(ValueError, match="字符串数组"):
        load({"sensitive": "api_key"})
    with pytest.raises(ValueError, match="空或非法"):
        load({"sensitive": ["ok", "   "]})
    with pytest.raises(ValueError, match="布尔值"):
        load({"audit_sensitive": "yes"})
    with pytest.raises(ValueError, match="object 或 null"):
        load({"acc": [1, 2]})

    assert load({"sensitive": ["api_key"]}).governance.audit_sensitive is False
    assert load({"acc": None}).governance.acc is None
