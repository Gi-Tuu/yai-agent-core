"""S2 子员工委派的离线测试：不依赖网络 / API Key。

覆盖：子员工运行回流、工具白名单收窄、delegate 并行委派、深度上限、
默认只读工具启发式、未知工具上报。
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402
from shell.employee import (  # noqa: E402
    MAX_DEPTH,
    DelegationSupervisor,
    attach_delegate,
    build_worker_core,
    default_budget,
    default_read_tools,
    run_worker,
)

from yai_core import AgentCore, ModelResponse, build_spec  # noqa: E402
from yai_core.kernel.budget import Budget  # noqa: E402
from yai_core.llm.scripted import ScriptedModel  # noqa: E402
from yai_core.types import ToolCallRequest  # noqa: E402


def list_items() -> list[str]:
    """列出全部条目。"""
    return ["a", "b"]


def get_item(key: str) -> dict:
    """读取单个条目。"""
    return {"key": key, "value": key.upper()}


def delete_item(key: str) -> dict:
    """删除一个条目。"""
    return {"deleted": key}


def _root(responses: list[ModelResponse]) -> AgentCore:
    core = AgentCore(ScriptedModel(responses))
    core.register_tools(
        [build_spec(list_items), build_spec(get_item), build_spec(delete_item)]
    )
    return core


def test_worker_runs_and_returns() -> None:
    root = _root([ModelResponse(content="共 2 条")])

    result = asyncio.run(
        run_worker(root, goal="列出条目数量", tools=["list_items"])
    )

    assert result.ok
    assert result.final_text == "共 2 条"
    assert result.granted_tools == ["list_items"]


def test_worker_tools_are_narrowed() -> None:
    root = _root([ModelResponse(content="ok")])

    worker = build_worker_core(root, goal="列出条目", tools=["list_items"])

    assert worker.registry.has("list_items")
    assert not worker.registry.has("get_item")
    assert not worker.registry.has("delete_item")
    assert worker.policy.mode == "deny_all"


def test_delegate_parallel_returns_observations() -> None:
    root = _root(
        [
            ModelResponse(content="子结果一"),
            ModelResponse(content="子结果二"),
        ]
    )
    attach_delegate(root, depth=1)
    delegate = root.registry.get("delegate")

    out = asyncio.run(
        delegate.handler(
            tasks=[
                {"goal": "子目标一", "tools": ["list_items"]},
                {"goal": "子目标二", "tools": ["get_item"]},
            ]
        )
    )

    results = out["results"]
    assert [r["goal"] for r in results] == ["子目标一", "子目标二"]
    assert all(r["ok"] for r in results)
    assert results[0]["result"] == "子结果一"
    assert results[1]["granted_tools"] == ["get_item"]


def test_depth_limit_removes_delegate_at_max() -> None:
    root = _root([ModelResponse(content="ok")])

    w1 = build_worker_core(
        root, goal="x", tools=["list_items"], depth=1, register_delegate=True
    )
    w2 = build_worker_core(
        root,
        goal="x",
        tools=["list_items"],
        depth=MAX_DEPTH,
        register_delegate=True,
    )

    assert w1.registry.has("delegate")
    assert not w2.registry.has("delegate")


def test_default_read_tools_prefix() -> None:
    names = ["list_items", "get_item", "delete_item"]
    assert default_read_tools(names) == ["list_items", "get_item"]


def test_unknown_tool_reported_denied() -> None:
    root = _root([ModelResponse(content="我直接回答")])

    result = asyncio.run(
        run_worker(root, goal="汇报当前情况", tools=["ghost_tool"])
    )

    assert "ghost_tool" in result.denied


def test_worker_steps_recorded() -> None:
    root = _root(
        [
            ModelResponse(
                tool_calls=[
                    ToolCallRequest(id="c1", name="list_items", arguments={})
                ]
            ),
            ModelResponse(content="共 2 条"),
        ]
    )

    result = asyncio.run(run_worker(root, goal="列出条目", tools=["list_items"]))
    obs = result.to_observation()

    assert result.ok
    assert result.status == "done"
    assert result.steps == ["list_items"]
    assert obs["steps"] == ["list_items"]
    assert obs["status"] == "done"


def test_worker_timeout_marks_status() -> None:
    async def slow_tool() -> str:
        """故意长时间运行，用于触发墙钟超时。"""
        await asyncio.sleep(5)
        return "late"

    root = AgentCore(
        ScriptedModel(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCallRequest(id="c1", name="slow_tool", arguments={})
                    ]
                )
            ]
        )
    )
    root.register_tools([build_spec(slow_tool)])

    result = asyncio.run(
        run_worker(root, goal="调用慢工具并返回结果", tools=["slow_tool"], timeout=0.3)
    )

    assert result.status == "timeout"
    assert not result.ok
    # 内核在工具返回后才经执行器批量发出 tool_call；slow_tool 被超时取消、调用事件
    # 未及 yield，因此 steps 不记录这次未完成的调用（诚实反映事件时序）。
    assert result.steps == []


def test_on_event_callback_receives_progress() -> None:
    root = _root(
        [
            ModelResponse(
                tool_calls=[
                    ToolCallRequest(id="c1", name="list_items", arguments={})
                ]
            ),
            ModelResponse(content="done"),
        ]
    )
    seen: list[str] = []

    asyncio.run(
        run_worker(
            root,
            goal="列出条目",
            tools=["list_items"],
            on_event=lambda _goal, kind, _data: seen.append(kind),
        )
    )

    assert "tool_call" in seen
    assert "done" in seen


def test_delegate_timeout_does_not_block_gather() -> None:
    async def slow_tool() -> str:
        """故意长时间运行。"""
        await asyncio.sleep(5)
        return "late"

    class AlwaysSlow:
        """每次都要求调用 slow_tool（两个子员工共享，并行触发超时）。"""

        async def achat(self, messages, tools=None, *, tier: str = "standard"):
            return ModelResponse(
                tool_calls=[
                    ToolCallRequest(id="c", name="slow_tool", arguments={})
                ]
            )

    root = AgentCore(AlwaysSlow())
    root.register_tools([build_spec(slow_tool)])
    attach_delegate(root, depth=1)
    delegate = root.registry.get("delegate")

    loop = asyncio.new_event_loop()
    start = loop.time()
    out = loop.run_until_complete(
        delegate.handler(
            tasks=[
                {"goal": "慢一", "tools": ["slow_tool"], "timeout": 0.3},
                {"goal": "慢二", "tools": ["slow_tool"], "timeout": 0.3},
            ]
        )
    )
    elapsed = loop.time() - start
    loop.close()

    results = out["results"]
    assert [r["status"] for r in results] == ["timeout", "timeout"]
    assert all(not r["ok"] for r in results)
    # 关键：两个慢任务都被墙钟上限截断，gather 没有无限等待。
    assert elapsed < 3


def test_default_budget_derivation() -> None:
    budget = default_budget(3)
    # 模型调用按轮 + 收尾余量；工具按每轮最多 4 个并行放宽；token 默认不硬限。
    assert budget.max_model_calls == 5
    assert budget.max_tool_calls == 12
    assert budget.max_tokens is None
    assert budget.enabled


def test_worker_reports_budget_snapshot_on_done() -> None:
    root = _root([ModelResponse(content="ok")])

    result = asyncio.run(
        run_worker(root, goal="列出条目数量", tools=["list_items"], max_iters=3)
    )

    assert result.ok
    assert result.status == "done"
    assert result.budget is not None
    assert result.budget["limits"]["max_model_calls"] == 5
    assert result.budget["limits"]["max_tool_calls"] == 12
    # 预算快照随 observation 回流给根，便于根感知子员工花费。
    assert result.to_observation()["budget"] is result.budget


def test_worker_budget_exceeded_marks_status() -> None:
    # 模型连续两轮都要求调工具；工具上限 1，第二次工具调用前即熔断。
    root = _root(
        [
            ModelResponse(
                tool_calls=[ToolCallRequest(id="c1", name="list_items", arguments={})]
            ),
            ModelResponse(
                tool_calls=[ToolCallRequest(id="c2", name="list_items", arguments={})]
            ),
            ModelResponse(content="不应走到这里"),
        ]
    )

    result = asyncio.run(
        run_worker(
            root,
            goal="列出条目",
            tools=["list_items"],
            max_iters=5,
            budget=Budget(max_tool_calls=1),
        )
    )

    assert result.status == "budget_exceeded"
    assert not result.ok
    assert result.budget is not None
    assert result.budget["limits"]["max_tool_calls"] == 1
    assert result.budget["tool_calls"] == 1
    # 第一次工具已实际执行并记录步骤，熔断发生在第二次调用前，未产生额外副作用。
    assert result.steps == ["list_items"]


def test_shared_supervisor_caps_concurrency_across_levels() -> None:
    """根 delegate 与子员工 delegate 共用同一 supervisor 时，跨层级并发峰值 ≤ 上限。

    若每层各建一把信号量（回归到旧行为），根 2 个 + 子 2 个会同时占用 -> peak=4；
    共享全局闸时 peak 必须等于上限 2。
    """
    state = {"active": 0, "peak": 0}
    lock = asyncio.Lock()

    async def occupy_seat() -> dict:
        """占座并列出状态：执行期间持有委派并发名额。"""
        async with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.2)
        async with lock:
            state["active"] -= 1
        return {"occupied": True}

    class HoldModel:
        """无状态：没拿到工具结果就调 occupy_seat，拿到后即收尾，可被多员工共享。"""

        async def achat(self, messages, tools=None, *, tier: str = "standard"):
            last = messages[-1] if messages else None
            if getattr(last, "role", None) == "tool":
                return ModelResponse(content="占座完成")
            return ModelResponse(
                tool_calls=[
                    ToolCallRequest(id="c", name="occupy_seat", arguments={})
                ]
            )

    root = AgentCore(HoldModel())
    root.register_tools([build_spec(occupy_seat)])
    supervisor = DelegationSupervisor(max_concurrent=2)
    attach_delegate(root, depth=1, supervisor=supervisor)
    root_delegate = root.registry.get("delegate")

    child = build_worker_core(
        root,
        goal="列出占座情况",
        tools=["occupy_seat"],
        depth=1,
        register_delegate=True,
        supervisor=supervisor,
    )
    child_delegate = child.registry.get("delegate")

    async def scenario() -> None:
        await asyncio.gather(
            root_delegate.handler(
                tasks=[
                    {"goal": "列出占座情况", "tools": ["occupy_seat"]},
                    {"goal": "列出占座情况", "tools": ["occupy_seat"]},
                ]
            ),
            child_delegate.handler(
                tasks=[
                    {"goal": "列出占座情况", "tools": ["occupy_seat"]},
                    {"goal": "列出占座情况", "tools": ["occupy_seat"]},
                ]
            ),
        )

    loop = asyncio.new_event_loop()
    loop.run_until_complete(scenario())
    loop.close()

    assert state["peak"] == 2
    assert state["active"] == 0


def test_worker_cancellation_propagates() -> None:
    async def slow_tool() -> str:
        """长时间运行，用于验证外层取消能干净穿透子员工。"""
        await asyncio.sleep(5)
        return "late"

    root = AgentCore(
        ScriptedModel(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCallRequest(id="c1", name="slow_tool", arguments={})
                    ]
                )
            ]
        )
    )
    root.register_tools([build_spec(slow_tool)])

    async def scenario() -> None:
        task = asyncio.ensure_future(
            run_worker(root, goal="调用慢工具并返回结果", tools=["slow_tool"], timeout=10)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["deny", "ask"])
def test_worker_cannot_relax_parent_policy(decision) -> None:
    from yai_core.spi import PermissionDecision

    calls = []

    def remove_record() -> str:
        calls.append(1)
        return "removed"

    class Policy:
        async def check(self, name, arguments):
            return PermissionDecision(decision)

    root = AgentCore(ScriptedModel([
        ModelResponse(tool_calls=[ToolCallRequest(id="c", name="remove_record", arguments={})]),
        ModelResponse(content="finished"),
    ]), policy=Policy())
    root.register_tools([build_spec(remove_record)])
    result = asyncio.run(run_worker(root, goal="删除记录", tools=["remove_record"]))
    assert calls == []
    assert "remove_record" in result.denied
    assert not result.ok


@pytest.mark.parametrize("kind", ["approval", "gate"])
def test_worker_never_auto_confirms_required_approval(kind) -> None:
    from yai_core.types import ApprovalPolicy, GateDecision, ToolGovernance

    calls = []

    def remove_record() -> str:
        calls.append(1)
        return "removed"

    spec = build_spec(remove_record)
    spec.governance = (
        ToolGovernance(approval=ApprovalPolicy(required=True)) if kind == "approval"
        else ToolGovernance(gate=lambda args, subject: GateDecision(require_approval=True))
    )
    root = AgentCore(ScriptedModel([
        ModelResponse(tool_calls=[ToolCallRequest(id="c", name=spec.name, arguments={})]),
        ModelResponse(content="finished"),
    ]))
    root.register_tools([spec])
    result = asyncio.run(run_worker(root, goal="删除记录", tools=[spec.name]))
    assert calls == []
    assert spec.name in result.denied
    assert not result.ok


def test_worker_rechecks_parent_policy_after_assembly() -> None:
    from yai_core.policy import AllowlistPolicy

    root = _root([ModelResponse(content="ok")])
    worker = build_worker_core(root, goal="读取", tools=["get_item"])
    root.executor.policy = AllowlistPolicy(mode="deny_all")
    _, ok, _ = asyncio.run(worker.executor.execute("get_item", {"key": "private"}))
    assert not ok


def test_requested_delegate_is_rebound_and_cannot_escape_depth() -> None:
    root = _root([ModelResponse(content="ok")])
    attach_delegate(root)
    child = build_worker_core(
        root, goal="子任务", tools=["list_items", "delegate"], register_delegate=True
    )
    assert child.registry.get("delegate") is not root.registry.get("delegate")
    leaf = build_worker_core(
        child, goal="孙任务", tools=["list_items", "delegate"], depth=MAX_DEPTH,
        register_delegate=True,
    )
    assert not leaf.registry.has("delegate")


def test_empty_delegate_allowlist_does_not_grant_defaults() -> None:
    root = _root([ModelResponse(content="ok")])
    attach_delegate(root)
    out = asyncio.run(root.registry.get("delegate").handler(
        tasks=[{"goal": "no tools", "tools": []}]
    ))
    assert out["results"][0]["granted_tools"] == []


@pytest.mark.parametrize("subject_id", ["permitted", "blocked"])
def test_delegate_inherits_trusted_task_subject(subject_id) -> None:
    from yai_core.spi import PermissionDecision
    from yai_core.types import Subject, ToolGovernance

    seen = []
    called = []
    subject = Subject(subject_id=subject_id)

    class Policy:
        yai_subject_aware = True

        async def check(self, name, arguments, *, subject=None):
            seen.append((name, subject))
            if name == "delegate" or subject.subject_id == "permitted":
                return PermissionDecision.ALLOW
            return PermissionDecision.DENY

    def get_private() -> str:
        called.append(1)
        return "private result"

    gate_subjects = []
    spec = build_spec(get_private)
    spec.governance = ToolGovernance(gate=lambda args, who: gate_subjects.append(who))
    root = AgentCore(ScriptedModel([
        ModelResponse(tool_calls=[ToolCallRequest(id="d", name="delegate", arguments={
            "tasks": [{"goal": "读取", "tools": [spec.name], "subject": "permitted"}],
        })]),
        ModelResponse(tool_calls=[ToolCallRequest(id="c", name=spec.name, arguments={})]),
        ModelResponse(content="child finished"),
        ModelResponse(content="root finished"),
    ]), policy=Policy(), llm_router=False)
    root.register_tools([spec])
    attach_delegate(root)
    asyncio.run(root.run("委派读取资料", subject=subject))
    assert (spec.name, subject) in seen
    assert gate_subjects and all(who is subject for who in gate_subjects)
    assert called == ([1] if subject_id == "permitted" else [])
    assert root._task_subject is None


def test_grandchild_uses_all_ancestor_argument_policies() -> None:
    from yai_core.spi import PermissionDecision

    class Policy:
        async def check(self, name, arguments):
            return (PermissionDecision.DENY if arguments.get("key") == "private"
                    else PermissionDecision.ALLOW)

    root = _root([ModelResponse(content="ok")])
    root.executor.policy = Policy()
    child = build_worker_core(root, goal="子", tools=["get_item"])
    leaf = build_worker_core(child, goal="孙", tools=["get_item"], depth=MAX_DEPTH)
    _, allowed, _ = asyncio.run(leaf.executor.execute("get_item", {"key": "public"}))
    _, denied, _ = asyncio.run(leaf.executor.execute("get_item", {"key": "private"}))
    assert allowed and not denied


def test_rebound_delegate_preserves_parent_approval() -> None:
    from yai_core.types import ApprovalPolicy, ToolGovernance

    root = _root([ModelResponse(content="ok")])
    attach_delegate(root)
    governance = ToolGovernance(approval=ApprovalPolicy(required=True))
    root.registry.get("delegate").governance = governance
    child = build_worker_core(root, goal="子", tools=["get_item"], register_delegate=True)
    assert child.registry.get("delegate").governance is governance
    _, ok, _ = asyncio.run(child.executor.execute("delegate", {
        "tasks": [{"goal": "孙", "tools": ["get_item"]}]
    }))
    assert not ok


@pytest.mark.parametrize("case", ["gate", "missing_call", "unknown_allowlist"])
def test_worker_reports_failures_without_tool_events(case) -> None:
    from yai_core.types import GateDecision, ToolGovernance

    root = _root([
        ModelResponse(tool_calls=[ToolCallRequest(
            id="c", name="ghost" if case == "missing_call" else "list_items", arguments={}
        )]),
        ModelResponse(content="finished"),
    ])
    if case == "gate":
        root.registry.get("list_items").governance = ToolGovernance(
            gate=lambda args, subject: GateDecision(reject="blocked")
        )
    result = asyncio.run(run_worker(
        root, goal="读取", tools=["ghost"] if case == "unknown_allowlist" else ["list_items"]
    ))
    assert not result.ok
    assert result.denied


@pytest.mark.parametrize("case", ["approval", "gate", "missing", "timeout"])
def test_nested_worker_failure_is_not_reported_as_success(case) -> None:
    from yai_core.types import ApprovalPolicy, GateDecision, ToolGovernance

    calls = []

    async def remove_record() -> str:
        if case == "timeout":
            await asyncio.Event().wait()
        calls.append(1)
        return "removed"

    name = "ghost" if case == "missing" else "remove_record"
    root = AgentCore(ScriptedModel([
        ModelResponse(tool_calls=[ToolCallRequest(id="d", name="delegate", arguments={
            "tasks": [{"goal": "删除", "tools": [name], "timeout": 0.01}],
        })]),
        ModelResponse(tool_calls=[ToolCallRequest(id="c", name=name, arguments={})]),
        ModelResponse(content="grandchild finished"),
        ModelResponse(content="child finished"),
    ]))
    spec = build_spec(remove_record)
    if case == "approval":
        spec.governance = ToolGovernance(approval=ApprovalPolicy(required=True))
    elif case == "gate":
        spec.governance = ToolGovernance(gate=lambda args, subject: GateDecision(reject="blocked"))
    root.register_tools([spec])
    attach_delegate(root)
    result = asyncio.run(run_worker(
        root, goal="委派删除", tools=[spec.name], register_delegate=True
    ))
    assert calls == []
    assert not result.ok
    assert "delegate" in result.denied


def test_delegate_exception_cancels_and_joins_siblings(monkeypatch) -> None:
    import shell.employee.delegate as delegate_module

    async def scenario():
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def remove_record() -> str:
            started.set()
            try:
                await release.wait()
                calls.append(1)
                return "removed"
            finally:
                cancelled.set()

        root = AgentCore(ScriptedModel([
            ModelResponse(tool_calls=[ToolCallRequest(id="c", name="remove_record", arguments={})])
        ]))
        root.register_tools([build_spec(remove_record)])
        supervisor = attach_delegate(root)
        original = delegate_module.run_worker

        async def run(core, **kwargs):
            if kwargs["goal"] == "invalid":
                await started.wait()
            return await original(core, **kwargs)

        monkeypatch.setattr(delegate_module, "run_worker", run)
        with pytest.raises(ValueError):
            await root.registry.get("delegate").handler(tasks=[
                {"goal": "正常", "tools": ["remove_record"]},
                {"goal": "invalid", "max_iters": -1},
            ])
        assert cancelled.is_set()
        assert supervisor.semaphore._value == supervisor.max_concurrent
        release.set()
        assert calls == []

    asyncio.run(asyncio.wait_for(scenario(), 1))


def test_task_subject_cleared_when_stream_closed() -> None:
    from yai_core.types import Subject

    root = _root([ModelResponse(content="ok")])
    subject = Subject(subject_id="one-task-only")

    async def scenario():
        stream = root.astream("hello", subject=subject)
        await anext(stream)
        assert root._task_subject is subject
        await stream.aclose()
        assert root._task_subject is None

    asyncio.run(scenario())
