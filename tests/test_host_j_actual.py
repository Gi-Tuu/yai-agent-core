"""host_j（Actual Budget 记账宿主）离线测试：不联网、不需要 Node/actual CLI。

覆盖第三方播种"五拍"的两条标准链路与后端/工具层：

- Fake 内存账：整数分约定、未分类种子、预算超支判定、写操作改账；
- CLI 后端：argv 严格锁定官方 ``@actual-app/cli``（含 ``--format json``）、
  口令只走环境变量不进 argv、跨账户逐账户拉取本地聚合、错误体面抛出；
- 发现+授权链路：缺口 → 发现 suggest_category → 两次授权 → 补分类 → 预算核对；
- 授权被拒：新工具未获批即诚实停止、不产生写副作用；
- 委派链路：根一次 delegate 派两个白名单收窄的只读子员工，并行 ok、结果回流。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from shell.employee import run_worker  # noqa: E402

from five_beats import (  # noqa: E402
    assert_delegate_beats,
    assert_discover_beats,
    present_beats,
)
from host_j_actual import capabilities as cap  # noqa: E402
from host_j_actual.backend import (  # noqa: E402
    DEMO_MONTH,
    CliActualBackend,
    CliActualError,
    FakeActualBackend,
)
from host_j_actual.demo_agent import (  # noqa: E402
    DEFAULT_DELEGATE_TASK,
    DEFAULT_TASK,
    DelegatingReconcileModel,
    build_delegate_core,
    build_discovery_core,
)

# --------------------------------------------------------------------------- #
# Fake 后端：领域约定
# --------------------------------------------------------------------------- #


def test_fake_seed_data_uses_integer_cents_and_three_uncategorized() -> None:
    """种子账金额为整数分，且恰好 3 笔未分类（t3/t5/t6）。"""
    backend = FakeActualBackend()
    txs = {t["id"]: t for t in backend.transactions()}
    assert all(isinstance(t["amount_cents"], int) for t in txs.values())
    assert [t["id"] for t in txs.values() if t["category_id"] is None] == [
        "t3",
        "t5",
        "t6",
    ]
    # 娱乐预算 100 元，电影 128 元 → 超支 28 元（整数分）。
    assert txs["t8"]["amount_cents"] == -12800
    assert backend.budget_month(DEMO_MONTH)["c-fun"] == 10000


def test_set_category_and_add_expense_mutate_ledger() -> None:
    """写原语改账：补分类、记支出（元→分换算，支出为负）。"""
    backend = FakeActualBackend()
    updated = backend.set_transaction_category("t3", "c-food")
    assert updated["category_id"] == "c-food"
    tx = backend.add_transaction(
        "a-checking", "2026-09-30", -1250, "测试商户", category_id="c-food"
    )
    assert tx["amount_cents"] == -1250
    assert any(t["id"] == tx["id"] for t in backend.transactions())

    with pytest.raises(ValueError):
        backend.set_transaction_category("t3", "c-no-such")
    with pytest.raises(ValueError):
        backend.add_transaction("a-no-such", "2026-09-30", -1, "x")


# --------------------------------------------------------------------------- #
# 工具层
# --------------------------------------------------------------------------- #


def test_tools_budget_flags_fun_over_budget() -> None:
    """get_month_budget 仅标记娱乐超支；reconcile_overview 聚合未分类笔数。"""
    tools = cap.make_tools(FakeActualBackend())
    budget = tools.get_month_budget(DEMO_MONTH)
    by_cat = {line["category_id"]: line for line in budget["lines"]}
    assert by_cat["c-fun"]["over_budget"] is True
    assert by_cat["c-food"]["over_budget"] is False
    assert budget["over_budget_categories"] == ["娱乐"]

    overview = tools.reconcile_overview(DEMO_MONTH)
    assert overview["uncategorized_count"] == 3
    assert overview["account_count"] == 2
    assert overview["over_budget_categories"] == ["娱乐"]


def test_recent_transactions_lists_uncategorized_and_yuan() -> None:
    """交易列表标注未分类项，金额换算成元（星巴克 38 元）。"""
    tools = cap.make_tools(FakeActualBackend())
    recent = tools.list_recent_transactions(DEMO_MONTH)
    assert recent["uncategorized_ids"] == ["t3", "t5", "t6"]
    t3 = next(t for t in recent["transactions"] if t["id"] == "t3")
    assert t3["amount_yuan"] == -38.0
    assert t3["uncategorized"] is True


def test_suggest_category_hit_and_miss() -> None:
    """关键词建议命中餐饮；无关键词时诚实返回 None，不臆造分类。"""
    tools = cap.make_tools(FakeActualBackend())
    hit = tools.suggest_category("星巴克", "")
    assert hit["category_id"] == "c-food"
    miss = tools.suggest_category("某无法识别商户XYZ", "")
    assert miss["category_id"] is None
    assert miss["confidence"] == 0.0


def test_categorize_tool_clears_uncategorized() -> None:
    """经工具批量补分类后，未分类清零。"""
    backend = FakeActualBackend()
    tools = cap.make_tools(backend)
    result = tools.categorize_transactions(
        [
            {"tx_id": "t3", "category_id": "c-food"},
            {"tx_id": "t5", "category_id": "c-grocery"},
            {"tx_id": "t6", "category_id": "c-transit"},
        ]
    )
    assert result["updated_count"] == 3
    assert tools.reconcile_overview(DEMO_MONTH)["uncategorized_count"] == 0


def test_add_expense_tool_yuan_to_cents() -> None:
    """add_expense 入参元（正数），落库为负整数分。"""
    backend = FakeActualBackend()
    tools = cap.make_tools(backend)
    out = tools.add_expense("a-checking", "2026-09-29", 12.5, "午餐", category_id="c-food")
    assert out["amount_yuan"] == -12.5


# --------------------------------------------------------------------------- #
# CLI 后端：argv 锁定（注入 runner，不真的拉起子进程）
# --------------------------------------------------------------------------- #


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _cli(env=None, runner=None) -> CliActualBackend:
    # 注入 runner 时跳过环境变量齐备性校验，便于离线锁定 argv。
    return CliActualBackend(cli="actual", runner=runner, env=env or {})


def test_cli_accounts_argv_locked_json() -> None:
    captured: list[list[str]] = []

    def runner(argv, env):
        captured.append(argv)
        return _FakeProc(stdout=json.dumps([{"id": "a1", "balance_cents": 100}]))

    backend = _cli(runner=runner)
    rows = backend.accounts()
    assert rows == [{"id": "a1", "balance_cents": 100}]
    assert captured[-1] == ["actual", "accounts", "list", "--format", "json"]


def test_cli_secret_stays_in_env_not_argv() -> None:
    """口令只允许出现在环境变量，绝不进入命令参数。"""
    captured: list[list[str]] = []
    secret = "super-secret-token"

    def runner(argv, env):
        captured.append(argv)
        assert env.get("ACTUAL_SESSION_TOKEN") == secret
        return _FakeProc(stdout="[]")

    backend = _cli(
        env={"ACTUAL_SERVER_URL": "https://x", "ACTUAL_SYNC_ID": "s",
             "ACTUAL_SESSION_TOKEN": secret},
        runner=runner,
    )
    backend.accounts()
    assert all(secret not in part for part in captured[-1])


def test_cli_transactions_per_account_local_aggregation() -> None:
    """跨账户查询逐账户拉取后本地聚合，日期参数透传，不臆造 ActualQL。"""
    calls: list[list[str]] = []

    def runner(argv, env):
        calls.append(argv)
        if argv[1] == "accounts":
            return _FakeProc(stdout=json.dumps([{"id": "a1"}, {"id": "a2"}]))
        # argv = [cli, "transactions", "list", "--account", <id>, ...]
        return _FakeProc(stdout=json.dumps([{"id": argv[4], "amount_cents": -1}]))

    backend = _cli(runner=runner)
    rows = backend.transactions(start="2026-09-01", end="2026-09-30")
    assert {r["id"] for r in rows} == {"a1", "a2"}
    # 1 次 accounts + 每账户 1 次 transactions。
    assert len(calls) == 3
    tx_call = next(c for c in calls if c[1] == "transactions")
    assert tx_call[:4] == ["actual", "transactions", "list", "--account"]
    assert "--start" in tx_call and "--end" in tx_call
    assert tx_call[tx_call.index("--end") + 1] == "2026-09-30"
    # _json 统一在末尾追加 --format json。
    assert tx_call[-2:] == ["--format", "json"]


def test_cli_nonzero_exit_and_bad_json_raise() -> None:
    backend = _cli(runner=lambda argv, env: _FakeProc(stderr="boom", returncode=2))
    with pytest.raises(CliActualError):
        backend.accounts()
    backend2 = _cli(runner=lambda argv, env: _FakeProc(stdout="not-json"))
    with pytest.raises(CliActualError):
        backend2.accounts()


def test_cli_requires_env_without_injected_runner() -> None:
    """真机实例化（无 runner）缺少配置应尽早报错。"""
    with pytest.raises(CliActualError):
        CliActualBackend(runner=None, env={})


# --------------------------------------------------------------------------- #
# 发现 + 授权链路（五拍）
# --------------------------------------------------------------------------- #


def _run(coro):
    return asyncio.run(coro)


def test_discovery_beats_classify_with_approval() -> None:
    """批准授权：五拍齐全，3 笔未分类被补分类，最终月报非空。"""
    backend = FakeActualBackend()
    core = build_discovery_core(backend, auto_confirm=True)

    result = _run(core.run(DEFAULT_TASK))
    assert_discover_beats(result.events)

    # 两次授权：新工具 suggest_category + 写操作 categorize_transactions。
    asked = {
        e.data.get("tool")
        for e in result.events
        if getattr(e.type, "value", str(e.type)) == "permission_asked"
    }
    assert "suggest_category" in asked
    assert "categorize_transactions" in asked

    cats = {t["id"]: t["category_id"] for t in backend.transactions()}
    assert cats["t3"] == "c-food"
    assert cats["t5"] == "c-grocery"
    assert cats["t6"] == "c-transit"
    assert "超支" in result.final_text or "娱乐" in result.final_text


def test_discovery_denied_leaves_ledger_untouched() -> None:
    """拒绝授权：发现的新工具未获批即停止，未分类保持原样、无写副作用。"""
    backend = FakeActualBackend()
    core = build_discovery_core(backend, auto_confirm=False, deny=True)

    result = _run(core.run(DEFAULT_TASK))
    asked = [
        e.data.get("tool")
        for e in result.events
        if getattr(e.type, "value", str(e.type)) == "permission_asked"
    ]
    assert "suggest_category" in asked
    # 未分类一笔都没被改。
    assert all(
        t["category_id"] is None
        for t in backend.transactions()
        if t["id"] in {"t3", "t5", "t6"}
    )
    assert result.final_text  # 诚实停止文案非空


# --------------------------------------------------------------------------- #
# 委派链路（五拍：派子员工）
# --------------------------------------------------------------------------- #


def test_delegate_beats_two_readonly_workers_succeed() -> None:
    """根一次 delegate 派两个只读子员工，白名单收窄、并行 ok、结果只回流根。"""
    backend = FakeActualBackend()
    core = build_delegate_core(backend)

    result = _run(core.run(DEFAULT_DELEGATE_TASK))
    assert_delegate_beats(result.events)

    # delegate 只被根调用一次（拿到回流即收尾，不重复派单）。
    delegate_calls = [
        e
        for e in result.events
        if getattr(e.type, "value", str(e.type)) == "tool_call"
        and e.data.get("tool") == "delegate"
    ]
    assert len(delegate_calls) == 1

    # 回流 preview 在内核里被截断到定长，不能整体 JSON 解析；端到端只断言它
    # 明确带回了"子员工成功"的信号（两个子员工各自成功由下面的直驱测试严格验证）。
    delegate_result = next(
        e
        for e in result.events
        if getattr(e.type, "value", str(e.type)) == "tool_result"
        and e.data.get("tool") == "delegate"
    )
    assert delegate_result.data.get("ok") is True
    assert '"ok": true' in delegate_result.data["preview"]

    # 委派全程只读：未分类不被改动，根给出汇总。
    assert all(
        t["category_id"] is None
        for t in backend.transactions()
        if t["id"] in {"t3", "t5", "t6"}
    )
    assert result.final_text


def test_each_worker_succeeds_with_narrowed_single_tool() -> None:
    """直接驱动两个子员工：白名单收窄到单一只读工具，各调一次即成功收尾。"""

    async def _run():
        backend = FakeActualBackend()
        root = build_delegate_core(backend)
        cases = [
            (DelegatingReconcileModel.WORKER_GOAL_ACCOUNTS, "list_accounts"),
            (DelegatingReconcileModel.WORKER_GOAL_OVERVIEW, "reconcile_overview"),
        ]
        results = []
        for goal, tool in cases:
            results.append(
                await run_worker(
                    root, goal=goal, tools=[tool], depth=1, register_delegate=True
                )
            )
        return results

    workers = asyncio.run(_run())
    assert len(workers) == 2
    for w, tool in zip(
        workers, ["list_accounts", "reconcile_overview"], strict=True
    ):
        assert w.status == "done"
        assert w.ok is True
        assert w.granted_tools == [tool]
        assert w.steps == [tool]
        assert w.final_text  # 有小结回流给根


def test_delegate_worker_goals_are_distinct_and_readonly() -> None:
    """两个子任务目标不同、各自只授权一个只读工具（防串任务）。"""
    backend = FakeActualBackend()
    core = build_delegate_core(backend)
    result = _run(core.run(DEFAULT_DELEGATE_TASK))
    delegate_call = next(
        e
        for e in result.events
        if getattr(e.type, "value", str(e.type)) == "tool_call"
        and e.data.get("tool") == "delegate"
    )
    tasks = delegate_call.data["arguments"]["tasks"]
    goals = [t["goal"] for t in tasks]
    assert len(set(goals)) == 2
    assert all(t["tools"] == ["list_accounts"] or t["tools"] == ["reconcile_overview"]
               for t in tasks)
    # 五拍集合里不应出现"发现"（委派链路走的是派子员工而非补新工具）。
    assert "discover" not in present_beats(result.events)
