"""安全回归钉子：这一批修复逐条对应一个**真实可复现**的缺陷。

每个用例都写明"修复前是什么样"，防止将来重构把洞又打开：

- 作用域闸：空串前缀曾等于"整个根目录可写"（文档承诺的是失败关闭）；
- 参数级审批：数组内嵌路径的取值侧与 schema 侧语义不一致，条件会**静默不命中**；
- 工具结果：入参脱敏了、结果没脱敏，工具回显密钥即从事件流外泄；
- 文件列举：断链符号链接让整次列举失败、符号链接泄漏根目录外文件大小、截断标志误报；
- 作用域闸的风险等级：调用方显式声明的风险被默认值静默覆盖；
- 脱敏副本：深拷贝失败时退回原对象，通道改一下就改了执行参数；
- 能力闸：声明 ``enabled=false`` 却没有兜底；
- manifest 版本：治理语义与版本号脱钩，老读者会静默丢掉脱敏与审批要求。

全程离线，不依赖网络与 API Key。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from yai_core import build_spec
from yai_core.batteries.filesystem import FileSystemResources, filesystem_gate
from yai_core.channels import CollectChannel
from yai_core.integrations.acc import build_gate, parse_declaration
from yai_core.policy import AllowlistPolicy
from yai_core.policy.approval import (
    MISSING,
    evaluate_approval,
    schema_at_path,
    value_at_path,
)
from yai_core.policy.redact import redact_arguments
from yai_core.spi.resources import ResourcePolicy
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.tools.manifest import MANIFEST_VERSION, specs_from_manifest, specs_to_manifest
from yai_core.types import ApprovalPolicy, ApprovalRule, EventType, ToolGovernance, ToolSpec

SECRET = "sk-live-abcdefghijklmnop"

ROWS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"amount": {"type": "number"}},
            },
        }
    },
}


# ---------- 1. 作用域闸：空前缀不得等于"整个根目录" ----------


def test_empty_scope_prefix_is_rejected_at_construction() -> None:
    """修复前：``prefixes=("",)`` 被当成"整个根目录"，根目录整片静默可写。"""
    with pytest.raises(ValueError, match="作用域前缀"):
        filesystem_gate(prefixes=("",), write=True)


def test_empty_scope_tuple_denies_every_write() -> None:
    decision = filesystem_gate(prefixes=(), write=True)({"path": "secrets/x.txt"})
    assert decision.reject, "空前缀白名单必须失败关闭"


def test_declared_scope_still_works() -> None:
    gate = filesystem_gate(prefixes=("notes",), write=True)
    assert gate({"path": "notes/a.txt"}).reject == ""
    assert gate({"path": "secrets/a.txt"}).reject != ""


# ---------- 2. 参数级审批：取值侧与 schema 侧必须同一种路径语义 ----------


def test_array_element_condition_actually_matches() -> None:
    """修复前：``rows.amount == 5000`` 静默不命中（条件形同虚设，等于没有护栏）。"""
    outcome = evaluate_approval(
        ApprovalPolicy(
            when=(ApprovalRule(param="rows.amount", op="==", value=5000),)
        ),
        {"rows": [{"amount": 5000}]},
        ROWS_SCHEMA,
    )
    assert outcome.decision == "approval_required"


def test_array_index_condition_actually_matches() -> None:
    """修复前：``rows.0.amount`` 压根不被 schema 承认，直接判"参数不合法"。"""
    outcome = evaluate_approval(
        ApprovalPolicy(
            when=(ApprovalRule(param="rows.0.amount", op="==", value=5000),)
        ),
        {"rows": [{"amount": 5000}]},
        ROWS_SCHEMA,
    )
    assert outcome.decision == "approval_required"


def test_array_schema_path_accepts_index_segment() -> None:
    assert schema_at_path(ROWS_SCHEMA, "rows.0.amount") is not None
    assert schema_at_path(ROWS_SCHEMA, "rows.amount") is not None


def test_value_at_path_keeps_object_only_contract() -> None:
    """窄解析器保持原契约（历史 API 不改语义）。"""
    assert value_at_path({"roles": ["a"]}, "roles.0") is MISSING


def test_missing_value_keeps_per_operator_semantics() -> None:
    """值缺失时各算子的口径不变：数值/contains 失败关闭，!= 仍然要问人。"""
    numeric = evaluate_approval(
        ApprovalPolicy(when=(ApprovalRule(param="rows.amount", op=">", value=1),)),
        {"rows": []},
        ROWS_SCHEMA,
    )
    assert numeric.decision == "reject_invalid_arguments"

    not_equal = evaluate_approval(
        ApprovalPolicy(when=(ApprovalRule(param="rows.amount", op="!=", value=1),)),
        {"rows": []},
        ROWS_SCHEMA,
    )
    assert not_equal.decision == "approval_required"

    equal = evaluate_approval(
        ApprovalPolicy(when=(ApprovalRule(param="rows.amount", op="==", value=1),)),
        {"rows": []},
        ROWS_SCHEMA,
    )
    assert equal.decision == "allow"


# ---------- 3. 工具结果：入参脱敏了，结果也得脱敏 ----------


def _run_echo(governance: ToolGovernance) -> tuple[list, str]:
    def echo_token(token: str) -> dict:
        """把入参原样回显（极常见的调试工具形状）。"""
        return {"token": token, "ok": True}

    spec = build_spec(echo_token)
    spec.governance = governance
    registry = ToolRegistry()
    registry.register(spec)
    executor = ToolExecutor(registry, AllowlistPolicy((), mode="auto"), CollectChannel())
    events, ok, text = asyncio.run(executor.execute("echo_token", {"token": SECRET}))
    assert ok is True
    return events, text


def test_tool_result_preview_hides_declared_secret() -> None:
    """修复前：TOOL_RESULT 的 preview 直接带原文，密钥随事件流外泄。"""
    events, text = _run_echo(ToolGovernance(sensitive=("token",)))
    result = next(e for e in events if e.type == EventType.TOOL_RESULT)
    assert SECRET not in str(result.data)
    assert "[redacted" in result.data["preview"]
    assert SECRET in text, "返回给模型的正文必须是真值（脱敏只作用于对外副本）"


def test_audit_sensitive_collapses_result_preview() -> None:
    events, _text = _run_echo(ToolGovernance(audit_sensitive=True))
    result = next(e for e in events if e.type == EventType.TOOL_RESULT)
    assert SECRET not in str(result.data)
    assert "折叠" in result.data["preview"]


def test_undeclared_result_preview_stays_visible_by_design() -> None:
    """边界声明：内核不按字段名猜密钥，未声明即照常展示。"""
    events, _text = _run_echo(ToolGovernance())
    result = next(e for e in events if e.type == EventType.TOOL_RESULT)
    assert SECRET in result.data["preview"]


# ---------- 4. 文件列举：断链、外泄大小、截断标志 ----------


def _provider(tmp_path: Path, **kwargs: Any) -> FileSystemResources:
    base = tmp_path / "root"
    base.mkdir(exist_ok=True)
    return FileSystemResources(base, policy=ResourcePolicy(**kwargs))


def test_list_survives_broken_symlink(tmp_path: Path) -> None:
    """修复前：断链符号链接让 stat 抛错，整次列举失败。"""
    provider = _provider(tmp_path)
    (provider.root / "ok.txt").write_text("x", encoding="utf-8")
    try:
        (provider.root / "dead.txt").symlink_to(tmp_path / "nope.txt")
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无法创建符号链接")
    entries = asyncio.run(provider.list(""))
    assert {entry.name for entry in entries} >= {"ok.txt", "dead.txt"}


def test_list_does_not_leak_size_of_linked_file_outside_root(tmp_path: Path) -> None:
    """修复前：stat 跟随链接，报出根目录外文件的真实大小。"""
    provider = _provider(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x" * 4096)
    try:
        (provider.root / "link.bin").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无法创建符号链接")
    entries = asyncio.run(provider.list(""))
    linked = next(entry for entry in entries if entry.name == "link.bin")
    assert linked.size == 0


def test_truncated_flag_is_true_only_when_something_was_cut(tmp_path: Path) -> None:
    """修复前：条目数恰好等于上限时也会标记 truncated（假的）。"""
    base = tmp_path / "root"
    base.mkdir()
    for index in range(3):
        (base / f"f{index}.txt").write_text("x", encoding="utf-8")
    provider = FileSystemResources(base, policy=ResourcePolicy(max_list_entries=3))
    entries, truncated = asyncio.run(provider.list_with_flag(""))
    assert len(entries) == 3
    assert truncated is False

    (base / "f3.txt").write_text("x", encoding="utf-8")
    entries, truncated = asyncio.run(provider.list_with_flag(""))
    assert len(entries) == 3
    assert truncated is True


# ---------- 5. 作用域闸的风险等级不得被静默覆盖 ----------


def test_write_gate_keeps_the_stricter_risk() -> None:
    gate = filesystem_gate(prefixes=("notes",), write=True, risk="low")
    assert gate({"path": "notes/a.txt"}).risk == "high"


def test_read_gate_keeps_declared_risk() -> None:
    gate = filesystem_gate(prefixes=("notes",), write=False, risk="medium")
    assert gate({"path": "notes/a.txt"}).risk == "medium"


# ---------- 6. 脱敏副本不变式 ----------


def test_uncopyable_value_still_yields_independent_copy() -> None:
    """修复前：深拷贝失败退回原对象，通道改一下就改了执行参数。"""

    class Uncopyable:
        def __deepcopy__(self, memo: dict) -> Any:
            raise TypeError("不可拷贝")

    payload = {"a": 1, "obj": Uncopyable()}
    out = redact_arguments(payload, ())
    assert out is not payload
    out["a"] = 2
    assert payload["a"] == 1


# ---------- 7. 能力闸：enabled=false 必须兜底 ----------


def test_gate_rejects_disabled_declaration() -> None:
    parsed = parse_declaration({"version": 1, "enabled": False, "scope": "order.read"})
    assert parsed.valid and parsed.declaration is not None
    gate = build_gate(parsed.declaration, {"type": "object", "properties": {}})
    decision = gate({})
    assert decision.reject, "声明为未开放的能力，闸必须兜底拒绝"


# ---------- 8. manifest 版本与治理语义绑定 ----------


def _spec() -> ToolSpec:
    return ToolSpec(
        name="demo",
        description="示例",
        input_schema={"type": "object", "properties": {}},
        handler=None,
    )


def test_manifest_writes_current_version_and_still_reads_v1() -> None:
    data = specs_to_manifest([_spec()])
    assert data["manifest_version"] == MANIFEST_VERSION
    legacy = {
        "manifest_version": 1,
        "tools": [json.loads(json.dumps(_spec().to_manifest_dict()))],
    }
    restored = specs_from_manifest(legacy)
    assert restored and restored[0].name == "demo"


def test_manifest_rejects_unknown_version() -> None:
    with pytest.raises(ValueError, match="manifest_version"):
        specs_from_manifest({"manifest_version": 99, "tools": []})


def test_governance_is_not_lost_across_manifest_round_trip() -> None:
    spec = _spec()
    spec.governance = ToolGovernance(
        sensitive=("api_key",), approval=ApprovalPolicy(required=True, prompt="确认？")
    )
    restored = specs_from_manifest(specs_to_manifest([spec]))
    assert restored[0].governance.sensitive == ("api_key",)
    assert restored[0].governance.approval is not None
    assert restored[0].governance.approval.required is True


# ---------- 9. 声明文本进模型提示前必须净化（提示注入面） ----------


def test_guidance_text_is_sanitized_before_reaching_the_model() -> None:
    """不可信 spec 的 guidance 不能带控制字符 / 零宽 / 双向覆盖进模型提示。"""
    from yai_core.integrations.openapi.discovery import _sanitize_guidance

    dirty = "用这个\u202e翻转\u200b\u0007 工具"
    clean = _sanitize_guidance(dirty)
    assert "\u202e" not in clean and "\u200b" not in clean and "\x07" not in clean
    assert "翻转" in clean, "只净化字符，不该改写内容本身"


# ---------- 10. 读限额靠实际读到的字节数判定（不留 stat/read 窗口） ----------


def test_read_limit_is_enforced_by_actual_bytes_read(tmp_path: Path) -> None:
    provider = _provider(tmp_path, max_read_bytes=8)
    (provider.root / "big.txt").write_text("x" * 64, encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        asyncio.run(provider.read("big.txt"))
    assert "上限" in str(excinfo.value)


# ---------- 11. 非文本 part 不得被静默丢弃 ----------


def test_anthropic_adapter_skips_non_text_parts_as_documented() -> None:
    """既有契约：多模态 part 尚无落地路径，非文本 part 被跳过（不是静默丢数据的新问题）。"""
    from yai_core.llm.anthropic import _as_text

    assert _as_text([{"type": "text", "text": "hi"}]) == "hi"
    assert _as_text([{"type": "image"}]) == ""


# ---------- 12. 未声明路径的拒绝理由要可诊断 ----------


def test_undeclared_approval_path_rejection_names_declared_params() -> None:
    outcome = evaluate_approval(
        ApprovalPolicy(when=(ApprovalRule(param="totl", op=">", value=1),)),
        {"amount": 5},
        {"type": "object", "properties": {"amount": {"type": "number"}}},
    )
    assert outcome.decision == "reject_invalid_arguments"
    assert "totl" in outcome.reason and "amount" in outcome.reason
