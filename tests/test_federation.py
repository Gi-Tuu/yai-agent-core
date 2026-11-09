"""父子核 Phase 1（进程内）回归：方向性、grant、深度、环、策略与失败隔离。

不启动真实模型：父核用 ScriptedModel 扮演模型，子核只用工具（不需要模型）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from shell.federation import (
    ChildCore,
    Federation,
    FederationError,
    GrantDenied,
)

from yai_core import (
    AgentCore,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    build_spec,
)
from yai_core.channels import CollectChannel
from yai_core.policy import AllowlistPolicy


def _core(model=None):
    return AgentCore(model or ScriptedModel([ModelResponse(content="idle")]))


def _models(*calls: tuple[str, dict], final: str = "完成"):
    responses = [
        ModelResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"c{i}", name=name, arguments=args)],
        )
        for i, (name, args) in enumerate(calls)
    ]
    responses.append(ModelResponse(content=final))
    return ScriptedModel(responses)


def _document_child():
    """模拟"文档产品"里的子核：一个读写工具。"""
    written: list[tuple[str, str]] = []

    def append_entry(title: str, body: str = "") -> str:
        """向文档追加一条记录。"""
        written.append((title, body))
        return f"已写入 {title}"

    def read_entries() -> list[str]:
        """列出文档里的记录标题。"""
        return [title for title, _ in written]

    core = _core()
    core.register_tools([build_spec(append_entry), build_spec(read_entries)])
    return core, written


# ---------- 方向性与基本调用 ----------


def test_parent_calls_child_tool_through_prefixed_name() -> None:
    child_core, written = _document_child()
    parent = _core(
        _models(
            ("doc__append_entry", {"title": "ATM 流水", "body": "转出 100 元"}),
            final="已把流水记进文档。",
        )
    )
    federation = Federation(parent, node_id="atm")
    grant = federation.register_child(
        ChildCore(child_core, node_id="doc", child_id="doc-app", product="doc"),
        tools=["append_entry"],
    )

    result = asyncio.run(parent.run("把流水记到文档"))

    assert written == [("ATM 流水", "转出 100 元")]
    assert result.final_text == "已把流水记进文档。"
    assert grant.edge == "atm->doc"
    assert grant.tools == frozenset({"append_entry"})
    exposed = [tool["name"] for tool in parent.list_tools()]
    assert "doc__append_entry" in exposed
    # 未选中的工具不得出现在父核工具表里。
    assert "doc__read_entries" not in exposed


def test_register_all_tools_by_default() -> None:
    child_core, _written = _document_child()
    parent = _core()
    federation = Federation(parent, node_id="atm")
    grant = federation.register_child(ChildCore(child_core, node_id="doc", product="doc"))

    assert grant.tools == frozenset({"append_entry", "read_entries"})
    exposed = {tool["name"] for tool in parent.list_tools()}
    assert {"doc__append_entry", "doc__read_entries"} <= exposed


def test_host_can_invoke_child_without_model() -> None:
    child_core, written = _document_child()
    federation = Federation(_core(), node_id="atm")
    federation.register_child(
        ChildCore(child_core, node_id="doc", product="doc"), tools=["append_entry"]
    )

    outcome = asyncio.run(
        federation.invoke("doc", "append_entry", {"title": "手工写入", "body": "宿主直调"})
    )

    assert outcome["ok"] is True
    assert outcome["child"] == "doc"
    assert written == [("手工写入", "宿主直调")]


# ---------- grant ----------


def test_expired_grant_is_denied() -> None:
    child_core, written = _document_child()
    federation = Federation(_core(), node_id="atm")
    federation.register_child(
        ChildCore(child_core, node_id="doc", product="doc"),
        tools=["append_entry"],
        expires_in=0.0,
    )

    with pytest.raises(GrantDenied):
        asyncio.run(federation.invoke("doc", "append_entry", {"title": "过期"}))
    assert written == []


def test_grant_is_bound_to_child_identity() -> None:
    child_core, _written = _document_child()
    federation = Federation(_core(), node_id="atm")
    grant = federation.register_child(
        ChildCore(child_core, node_id="doc", child_id="doc-app", product="doc"),
        tools=["append_entry"],
    )

    with pytest.raises(GrantDenied):
        grant.check("other-app", "append_entry")


# ---------- 治理：父核策略先判，子核失败不拖垮父核 ----------


def test_parent_policy_can_deny_child_tool_before_it_runs() -> None:
    child_core, written = _document_child()
    parent = AgentCore(
        _models(("doc__append_entry", {"title": "不该写"}), final="被拒绝后继续。"),
        channel=CollectChannel(auto_confirm=False),
        policy=AllowlistPolicy((), mode="auto"),
    )
    Federation(parent, node_id="atm").register_child(
        ChildCore(child_core, node_id="doc", product="doc"), tools=["append_entry"]
    )

    result = asyncio.run(parent.run("尝试写入"))

    assert written == []  # 父核策略拒绝，子核根本没被调用
    assert result.final_text == "被拒绝后继续。"


def test_child_failure_is_isolated_and_visible() -> None:
    def boom() -> str:
        """总是失败的工具。"""
        raise RuntimeError("文档磁盘已满")

    child_core = _core()
    child_core.register_tools([build_spec(boom)])
    parent = _core(
        _models(("doc__boom", {}), final="记录失败，我稍后重试。")
    )
    Federation(parent, node_id="atm").register_child(
        ChildCore(child_core, node_id="doc", product="doc")
    )

    result = asyncio.run(parent.run("写一条记录"))

    assert result.final_text == "记录失败，我稍后重试。"
    assert result.status == "ok"


# ---------- 深度与环 ----------


def _echo_child(label: str):
    def echo(text: str = "") -> str:
        """原样返回文本。"""
        return f"{label}:{text}"

    core = _core()
    core.register_tools([build_spec(echo)])
    return core


def test_depth_limit_blocks_too_deep_chain() -> None:
    depth, max_depth = 3, 2
    root_federation = _build_chain(depth, max_depth=max_depth)
    # invoke 传的是 **b 核里的工具名**（c__d__echo），不是 a 核暴露后的前缀名。
    tool = "__".join(chr(ord("a") + i) for i in range(2, depth + 1)) + "__echo"
    prefix = chr(ord("a") + 1)

    with pytest.raises(FederationError) as excinfo:
        asyncio.run(root_federation.invoke(prefix, tool, {"text": "hi"}))
    assert "深度" in str(excinfo.value)


def test_cycle_is_detected() -> None:
    a_core = _echo_child("a")
    b_core = _core()
    b_fed = Federation(b_core, node_id="b")
    b_fed.register_child(ChildCore(a_core, node_id="a", product="a"), prefix="a")
    a_fed = Federation(a_core, node_id="a")
    a_fed.register_child(ChildCore(b_core, node_id="b", product="b"), prefix="b")

    with pytest.raises(FederationError) as excinfo:
        asyncio.run(a_fed.invoke("b", "a__echo", {"text": "loop"}))
    assert "循环" in str(excinfo.value)


def test_register_rejects_duplicate_prefix_and_unknown_tool() -> None:
    child_core, _written = _document_child()
    federation = Federation(_core(), node_id="atm")
    federation.register_child(ChildCore(child_core, node_id="doc", product="doc"))

    with pytest.raises(ValueError):
        federation.register_child(ChildCore(child_core, node_id="doc2", product="doc"))
    with pytest.raises(ValueError):
        Federation(_core(), node_id="atm2").register_child(
            ChildCore(child_core, node_id="doc", product="doc2"),
            tools=["does_not_exist"],
        )


def test_child_policy_denial_is_visible_and_fails_closed() -> None:
    """子核自己的策略拒绝时，父核拿到可见的工具错误，且子核工具不执行。"""
    calls: list[str] = []

    def append_entry(title: str = "") -> str:
        """向文档追加记录。"""
        calls.append(title)
        return f"已写入 {title}"

    child_core = AgentCore(
        ScriptedModel([ModelResponse(content="idle")]),
        channel=CollectChannel(auto_confirm=False),
        policy=AllowlistPolicy((), mode="auto"),  # 不在白名单 → 需要审批 → 无人确认
    )
    child_core.register_tools([build_spec(append_entry)])
    parent = _core(_models(("doc__append_entry", {"title": "x"}), final="子核拒绝了。"))
    Federation(parent, node_id="atm").register_child(
        ChildCore(child_core, node_id="doc", product="doc")
    )

    result = asyncio.run(parent.run("写一条记录"))

    assert calls == []  # 子核策略拒绝，工具没有执行
    assert result.final_text == "子核拒绝了。"


# ---------- 内部辅助 ----------


def _build_chain(depth: int, *, max_depth: int) -> Federation:
    """构建 a → b → c → … 的链，返回最外层 Federation。"""
    labels = [chr(ord("a") + i) for i in range(depth + 1)]
    # 叶子核：带一个 echo 工具
    leaf_core = _echo_child(labels[-1])
    cores: dict[str, AgentCore] = {labels[-1]: leaf_core}
    for label in labels[:-1]:
        cores[label] = _core()
    federations = {
        label: Federation(cores[label], node_id=label, max_depth=max_depth)
        for label in labels[:-1]
    }
    for i in range(len(labels) - 2, -1, -1):
        child_label = labels[i + 1]
        federations[labels[i]].register_child(
            ChildCore(cores[child_label], node_id=child_label, product=child_label),
            prefix=child_label,
        )
    return federations[labels[0]]


def test_example_host_m_federation_runs_offline() -> None:
    """示例必须永远可跑：README/文档里"父核调子核"的演示不能腐烂。"""
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(root / "examples" / "host_m_federation" / "run.py")],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    output = completed.stdout
    assert "doc__add_entry" in output
    assert "[父核→子核]" in output
    assert "[完成]" in output
    assert "ATM 流水 0001" in output
