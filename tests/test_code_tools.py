"""代码工具测试：注册表 + 48h TTL + 调用刷新 + 永久保留 + 沙箱执行 + 授权闸。

全部离线：假时钟、进程内 FakeSandbox、ScriptedModel，不依赖网络与 API Key。
内核不内置任何代码执行器，执行走宿主 ToolSandbox SPI。
"""

import asyncio
from datetime import UTC, datetime, timedelta

from yai_core import (
    CREATE_CODE_TOOL,
    AgentCore,
    CodeToolManager,
    ModelResponse,
    SandboxResult,
    ToolCallRequest,
    ToolExecutor,
    ToolRegistry,
    ToolSpec,
    build_spec,
)
from yai_core.channels.collect import CollectChannel
from yai_core.policy import AllowlistPolicy

TTL = 48 * 3600


class _Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t += timedelta(seconds=seconds)


def _make_manager(clock: _Clock) -> CodeToolManager:
    registry = ToolRegistry()
    return CodeToolManager(registry, clock=clock)


def _code_spec_body() -> dict:
    return {
        "name": "double",
        "description": "把输入数字翻倍",
        "input_schema": {
            "type": "object",
            "properties": {"x": {"type": "number"}},
            "required": ["x"],
        },
        "code": "def run(inputs):\n    return {'value': inputs['x'] * 2}",
    }


# ---------- 注册表与生命周期 ----------

def test_create_registers_code_spec() -> None:
    clock = _Clock()
    registry = ToolRegistry()
    manager = CodeToolManager(registry, clock=clock)
    body = _code_spec_body()
    spec = manager.create(**body)
    assert spec.source == "code" and spec.handler is None and spec.code == body["code"]
    assert registry.has("double")
    assert manager.is_live("double")
    assert manager.status()["total"] == 1


def test_create_rejects_duplicate_and_blank() -> None:
    manager = _make_manager(_Clock())
    manager.create(**_code_spec_body())
    import pytest

    with pytest.raises(ValueError):
        manager.create(**_code_spec_body())  # 重名
    for bad in (
        {"name": "", "description": "d", "input_schema": {}, "code": "x"},
        {"name": "n", "description": "", "input_schema": {}, "code": "x"},
        {"name": "n", "description": "d", "input_schema": {}, "code": ""},
    ):
        with pytest.raises(ValueError):
            manager.create(**bad)


def test_ttl_expires_after_48h_without_use() -> None:
    clock = _Clock()
    registry = ToolRegistry()
    manager = CodeToolManager(registry, clock=clock)
    manager.create(**_code_spec_body())

    clock.advance(TTL)
    assert manager.is_live("double")  # 恰好 48h（边界是 >）
    clock.advance(1)
    assert not manager.is_live("double")
    retired = manager.expire_stale()
    assert retired == ["double"]
    assert not registry.has("double")  # 过期后从注册表注销


def test_each_call_refreshes_ttl() -> None:
    clock = _Clock()
    manager = _make_manager(clock)
    manager.create(**_code_spec_body())

    clock.advance(47 * 3600)
    assert manager.touch("double")  # 第 1 次调用，刷新
    clock.advance(47 * 3600)
    assert manager.is_live("double")  # 距上次调用 47h，仍存活
    rec = manager.records()[0]
    assert rec["call_count"] == 1


def test_permanent_tool_never_expires() -> None:
    clock = _Clock()
    registry = ToolRegistry()
    manager = CodeToolManager(registry, clock=clock)
    manager.create(**_code_spec_body())
    assert manager.make_permanent("double") is True

    clock.advance(100 * TTL)
    assert manager.is_live("double")
    assert manager.expire_stale() == []
    assert registry.has("double")
    assert manager.records()[0]["permanent"] is True


def test_make_permanent_unknown_returns_false() -> None:
    manager = _make_manager(_Clock())
    assert manager.make_permanent("nope") is False


# ---------- 沙箱执行（FakeSandbox + AgentCore） ----------

class _FakeSandbox:
    def __init__(self, *, output=None, fail: str | None = None) -> None:
        self.calls: list[tuple] = []
        self._output = output
        self._fail = fail

    async def execute(self, code, inputs, *, timeout=None):
        self.calls.append((code, inputs, timeout))
        if self._fail:
            return SandboxResult(ok=False, error=self._fail)
        if self._output is not None:
            return SandboxResult(ok=True, output=self._output)
        return SandboxResult(ok=True, output={"echo": inputs})


class _CreateThenCallModel:
    """第 1 轮创建代码工具，第 2 轮调用它，第 3 轮收尾。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c1", name=CREATE_CODE_TOOL, arguments=_code_spec_body()
                    )
                ],
            )
        if self.calls == 2:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="d1", name="double", arguments={"x": 21}
                    )
                ],
            )
        return ModelResponse(content="翻倍结果是 42。")


def existing_tool() -> int:
    """一个占位原生工具，保证宿主初始就有工具。"""
    return 1


def test_create_code_tool_not_registered_without_sandbox() -> None:
    core = AgentCore(_CreateThenCallModel())
    core.register_tools([build_spec(existing_tool)])
    assert "create_code_tool" not in [t["name"] for t in core.list_tools()]
    assert core.code_tools_status()["enabled"] is False


def test_create_authorize_then_execute_in_sandbox() -> None:
    sandbox = _FakeSandbox(output={"value": 42})
    model = _CreateThenCallModel()
    core = AgentCore(model, sandbox=sandbox)
    core.register_tools([build_spec(existing_tool)])

    result = asyncio.run(core.run("帮我计算 21 的翻倍"))

    created = next(e for e in result.events if e.type.value == "code_tool_created")
    assert created.data["name"] == "double"
    assert created.data["ttl_seconds"] == TTL
    # 创建后下一轮真正调用，沙箱被执行
    calls = [e.data["tool"] for e in result.events if e.type.value == "tool_call"]
    assert calls == [CREATE_CODE_TOOL, "double"]
    assert len(sandbox.calls) == 1
    _, inputs, timeout = sandbox.calls[0]
    assert inputs == {"x": 21} and timeout is not None
    result_ev = next(e for e in result.events if e.type.value == "tool_result")
    assert result_ev.data["ok"] is True
    assert "42" in result.final_text
    # 注册表里能看到代码工具
    assert "double" in [t["name"] for t in core.list_tools()]
    assert core.code_tools_status()["live"] == 1


def test_create_code_tool_gated_by_permission() -> None:
    class _DenyChannel(CollectChannel):
        def __init__(self) -> None:
            super().__init__(auto_confirm=False)

        async def confirm(self, tool_name, arguments):
            return False

    model = _CreateThenCallModel()
    core = AgentCore(
        model,
        sandbox=_FakeSandbox(),
        auto_approve_tools=False,
        channel=_DenyChannel(),
    )
    core.register_tools([build_spec(existing_tool)])

    result = asyncio.run(core.run("帮我计算 21 的翻倍"))
    assert not [e for e in result.events if e.type.value == "code_tool_created"]
    assert "double" not in [t["name"] for t in core.list_tools()]


def test_sandbox_failure_reported_not_raised() -> None:
    sandbox = _FakeSandbox(fail="超时被终止")
    model = _CreateThenCallModel()
    core = AgentCore(model, sandbox=sandbox)
    core.register_tools([build_spec(existing_tool)])

    result = asyncio.run(core.run("帮我计算 21 的翻倍"))
    bad = [e for e in result.events if e.type.value == "tool_result" and not e.data["ok"]]
    assert bad and "超时" in bad[0].data["error"]
    assert result.events[-1].type.value == "done"


def test_executor_without_sandbox_degrades_gracefully() -> None:
    """注册了 code 工具但执行器没挂沙箱：优雅报能力缺失，不崩溃。"""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="c",
            description="代码工具",
            input_schema={"type": "object", "properties": {}},
            handler=None,
            source="code",
            code="def run(inputs):\n    return 1",
        )
    )
    executor = ToolExecutor(
        registry, AllowlistPolicy(mode="allow_all"), CollectChannel()
    )
    events, ok, text = asyncio.run(executor.execute("c", {}))
    assert ok is False and "沙箱" in text
    assert events and events[-1].data["ok"] is False


# ---------- 后台管理 ----------

def test_retain_and_sweep_via_core() -> None:
    sandbox = _FakeSandbox()
    core = AgentCore(_CreateThenCallModel(), sandbox=sandbox)
    core.register_tools([build_spec(existing_tool)])
    result = asyncio.run(core.run("帮我计算 21 的翻倍"))
    assert any(e.type.value == "code_tool_created" for e in result.events)

    # 后台永久保留
    assert core.retain_code_tool("double") is True
    assert core.code_tools_status()["permanent"] == 1
    # sweep 对永久工具不回收
    assert core.sweep_code_tools() == []
    # 未启用沙箱的 core 后台方法安全降级
    core2 = AgentCore(_CreateThenCallModel())
    assert core2.retain_code_tool("x") is False
    assert core2.sweep_code_tools() == []
