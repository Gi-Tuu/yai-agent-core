"""ACC v1 官方参考向量语料的落地测试：把向量喂进兼容层的公开接口，逐条比对期望结果。

语料出处与"这份语料证明什么"见 ``tests/fixtures/acc/SOURCE.md``。上游参考 oracle 用的是
完整 JSON Schema 校验器，本实现是零依赖子集校验器；向量所依赖的关键字（type / properties /
required / items）都在子集内，偏离只发生在向量没有覆盖的组合上，逐条记录在 ``LIMITATIONS``。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from yai_core.integrations.acc import (
    ACC_MAJOR_VERSION,
    approval_outcome,
    audit_sensitive,
    effective_risk,
    is_exposed,
    parse_declaration,
)

FIXTURE = Path(__file__).parent / "fixtures" / "acc" / "vectors.json"

# 上游 conformance/v1/vectors.json @ b5c48f6（Apache-2.0，规范版本 1.0.5），逐字节锁定。
FIXTURE_SHA256 = "c3d3943362aade6d4bbf5599edd2300895d1b7bee8cedec5604bff127a01db71"

CORPUS = json.loads(FIXTURE.read_text(encoding="utf-8"))
VECTORS: list[dict[str, Any]] = CORPUS["vectors"]

# 已知限制与刻意偏离：影响的是"向量之外"的组合，不影响本语料的任何一条。
LIMITATIONS = (
    "入参校验只实现 type/required/properties/items 四个关键字，enum/pattern/min-max 被忽略；",
    "GET + 显式 execution.readonly:false 时取 medium（oracle 取 low），按绑定文本的显式声明优先；",
    "timeout_ms / rate_limit 只解析不执行，尚未映射到 ToolExecutor 的超时与限流策略；",
    "subject.required 的可信主体由调用方以布尔传入，宿主侧主体注入尚未接入（任务 I）。",
)

_SUPPORTED_KINDS = frozenset({"declaration", "exposure", "approval", "risk-default", "audit-hint"})


def _observe(vector: dict[str, Any]) -> dict[str, Any]:
    """按向量的 kind 调用公开接口，产出与 expected 同键集的结果（键集不一致即失败）。"""
    raw = vector["input"]["declaration"]
    result = parse_declaration(raw)
    if vector["kind"] == "declaration":
        if result.valid and result.declaration is not None:
            return {"valid": True}
        return {"valid": False, "diagnostic": result.diagnostic}

    assert result.valid and result.declaration is not None, (
        f"runtime 向量携带非法声明：{result.errors}"
    )
    declaration = result.declaration
    inp = vector["input"]

    if vector["kind"] == "exposure":
        return {
            "exposed": is_exposed(
                declaration,
                scope_allowed=inp.get("scope_allowed") is True,
                trusted_subject=inp.get("trusted_subject") is True,
            )
        }
    if vector["kind"] == "approval":
        outcome = approval_outcome(declaration, inp.get("args") or {}, inp.get("parameter_schema"))
        return {"decision": outcome.decision}
    if vector["kind"] == "risk-default":
        return {"effective_risk": effective_risk(declaration, http_method=inp.get("http_method"))}
    if vector["kind"] == "audit-hint":
        return {"audit_sensitive": audit_sensitive(declaration)}
    raise AssertionError(f"语料出现本实现未覆盖的 kind：{vector['kind']}")


@pytest.mark.parametrize("vector", VECTORS, ids=[str(v["id"]) for v in VECTORS])
def test_acc_reference_vector(vector: dict[str, Any]) -> None:
    observed = _observe(vector)
    assert observed == vector["expected"], vector["assertion"]


def test_corpus_metadata() -> None:
    assert CORPUS["format_version"] == 1
    assert CORPUS["acc_version"] == ACC_MAJOR_VERSION
    assert CORPUS["specification_release"] == "1.0.5"


def test_corpus_bytes_are_the_vendored_revision() -> None:
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_corpus_inventory_and_unique_ids() -> None:
    ids = [str(v["id"]) for v in VECTORS]
    assert len(ids) == len(set(ids)) == 26
    kinds: dict[str, int] = {}
    for vector in VECTORS:
        kinds[str(vector["kind"])] = kinds.get(str(vector["kind"]), 0) + 1
    assert kinds == {
        "declaration": 5,
        "exposure": 4,
        "approval": 14,
        "risk-default": 2,
        "audit-hint": 1,
    }


def test_every_vector_kind_has_an_observation_branch() -> None:
    """上游若新增 kind，这里先红，避免"沉默通过"假装覆盖了全部语料。"""
    assert {str(v["kind"]) for v in VECTORS} == _SUPPORTED_KINDS


def test_profile_split_is_parser_five_runtime_twenty_one() -> None:
    """5 条 parser + 21 条 runtime：只声称 Parser Profile 时也必须把 runtime 原语跑通。"""
    profiles = [str(v["profile"]) for v in VECTORS]
    assert profiles.count("parser") == 5
    assert profiles.count("runtime") == 21
