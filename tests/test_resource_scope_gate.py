"""资源作用域闸（``filesystem_gate``）：审批目标必须等于执行目标。

钉住的语义：

- 归一化用**与 handler 同一个** ``normalize_relative``，所以"同一目标三种写法"
  不可能绕过白名单；
- 作用域外 / 非法形状 / 未配白名单的写 —— 全部**在闸口拒绝**，不过策略、
  不发 ``TOOL_CALL``、不碰文件系统；
- 前缀匹配是字面的：大小写差异**宁可误拒**；
- 闸只抬不放：它永远不替 ``PermissionPolicy`` 放行。

全程离线，只碰 tmp_path。
"""

from __future__ import annotations

import asyncio

from yai_core.batteries.filesystem import filesystem_capabilities, filesystem_gate
from yai_core.channels import CollectChannel
from yai_core.discovery.introspect import build_spec
from yai_core.policy import AllowlistPolicy
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import EventType, ToolGovernance

WRITABLE = ("notes/",)


def _decision(gate, path):
    return gate({"path": path}, None)


# ---------- 1. 归一化：同一目标、多种写法 ----------


def test_equivalent_spellings_normalize_to_one_target() -> None:
    gate = filesystem_gate(prefixes=WRITABLE, write=True)
    targets = {
        _decision(gate, spelling).target
        for spelling in ("notes/a.txt", "./notes/a.txt", "notes//a.txt", "notes/./a.txt")
    }
    assert targets == {"notes/a.txt"}


def test_case_difference_is_refused_not_guessed() -> None:
    """Windows/macOS 大小写不敏感，但白名单按字面匹配 —— 宁可误拒不可误放。"""
    gate = filesystem_gate(prefixes=WRITABLE, write=True)
    assert "不在允许的" in _decision(gate, "NOTES/a.txt").reject


# ---------- 2. 该拒的必须拒 ----------


def test_traversal_is_rejected_at_the_gate() -> None:
    gate = filesystem_gate(prefixes=WRITABLE, write=True)
    for bad in ("notes/../secrets/x", "../escape", "/etc/passwd", "C:/Windows/x"):
        assert _decision(gate, bad).reject, bad


def test_out_of_scope_is_rejected_with_a_model_readable_reason() -> None:
    gate = filesystem_gate(prefixes=WRITABLE, write=True)
    decision = _decision(gate, "secrets/token.txt")
    assert "secrets/token.txt" in decision.reject
    assert "notes/" in decision.reject  # 理由要说清允许什么，模型才知道怎么改


def test_write_without_any_scope_denies_everything() -> None:
    """空白名单 + write=True 是"不给写"，不是"随便写"。"""
    gate = filesystem_gate(prefixes=(), write=True)
    assert _decision(gate, "notes/a.txt").reject
    assert _decision(gate, "notes/a.txt").risk == "high"


def test_blank_path_for_write_is_rejected_not_crashed() -> None:
    gate = filesystem_gate(prefixes=WRITABLE, write=True)
    for bad in ("", "   ", None, 123, ["notes/a.txt"]):
        assert gate({"path": bad}, None).reject


def test_read_gate_defaults_to_root_scope() -> None:
    read = filesystem_gate(prefixes=WRITABLE)          # write=False
    assert read({"path": "notes/a.txt"}, None).target == "notes/a.txt"
    assert read({"path": ""}, None).target == ""       # file_list 的默认根目录
    assert read({"path": "secrets/x"}, None).reject


# ---------- 3. 端到端：闸在策略与 handler 之前 ----------


def _executor(tmp_path, *, prefixes, write_tools=("file_create", "file_patch")):
    """把四个 file_* 工具都注册上，并给**写类**工具挂作用域闸。

    注意 write_tools 必须同时含 file_create 与 file_patch：只挂一个的话，
    "越界写被拒"的测试会因为压根没有闸而假绿。
    """
    registry = ToolRegistry()
    for fn in filesystem_capabilities(tmp_path):
        spec = build_spec(fn)
        if fn.__name__ in write_tools:
            spec.governance = ToolGovernance(
                gate=filesystem_gate(prefixes=prefixes, write=True)
            )
        registry.register(spec)
    return ToolExecutor(registry, AllowlistPolicy(mode="allow_all"), CollectChannel())


def test_denied_write_never_touches_the_disk(tmp_path) -> None:
    (tmp_path / "notes").mkdir()
    executor = _executor(tmp_path, prefixes=("notes/",))
    events, ok, text = asyncio.run(
        executor.execute("file_create", {"path": "secrets/x.txt", "content": "boom"})
    )
    assert ok is False
    assert "作用域" in text
    assert not (tmp_path / "secrets").exists()
    assert not (tmp_path / "notes" / "x.txt").exists()
    # 拒绝先于策略与 handler：审计链上不该出现 TOOL_CALL
    assert not any(e.type == EventType.TOOL_CALL for e in events)


def test_allowed_write_reports_canonical_target(tmp_path) -> None:
    (tmp_path / "notes").mkdir()
    executor = _executor(tmp_path, prefixes=("notes/",))
    events, ok, _text = asyncio.run(
        executor.execute("file_create", {"path": "./notes//a.txt", "content": "hi"})
    )
    assert ok is True
    assert (tmp_path / "notes" / "a.txt").read_text(encoding="utf-8") == "hi"
    called = next(e for e in events if e.type == EventType.TOOL_CALL)
    # 审批与审计看到的是解析后的落点，不是模型原样写来的串
    assert called.data["target"] == "notes/a.txt"
    assert called.data["risk"] == "high"


def test_gate_cannot_override_host_denial(tmp_path) -> None:
    """闸只能抬不能放：策略 DENY 时，作用域内也照样不执行。"""
    (tmp_path / "notes").mkdir()
    executor = _executor(tmp_path, prefixes=("notes/",))
    executor.policy = AllowlistPolicy(allowed=(), mode="deny_all")
    events, ok, text = asyncio.run(
        executor.execute("file_create", {"path": "notes/a.txt", "content": "hi"})
    )
    assert ok is False
    assert "权限策略拒绝" in text
    assert not (tmp_path / "notes" / "a.txt").exists()


def test_without_the_gate_the_same_write_succeeds(tmp_path) -> None:
    """反"假绿"对照：越界写被拒必须是**闸在起作用**，而不是电池本来就不让写。

    去掉闸之后同一次调用会成功落盘 —— 说明"根目录内的作用域"这件事只有挂闸才有，
    这道闸补的是真实存在的缺口：文件系统那两道闸只管"出不出根目录"，
    管不了"根目录内的哪一块"。
    """
    (tmp_path / "notes").mkdir()
    registry = ToolRegistry()
    for fn in filesystem_capabilities(tmp_path):
        registry.register(build_spec(fn))  # 不挂任何闸
    executor = ToolExecutor(
        registry, AllowlistPolicy(mode="allow_all"), CollectChannel()
    )
    _events, ok, _text = asyncio.run(
        executor.execute("file_create", {"path": "secrets/x.txt", "content": "boom"})
    )
    assert ok is True
    assert (tmp_path / "secrets" / "x.txt").read_text(encoding="utf-8") == "boom"
