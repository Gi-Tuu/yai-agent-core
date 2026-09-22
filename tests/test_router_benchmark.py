"""自校准路由离线 benchmark 的测试（固定种子、不起进程、不依赖网络）。

scripts/ 不在包路径，这里用 importlib 按文件路径加载 benchmark_router，
并把 scripts/ 临时加入 sys.path 以解析其同目录的 router_bench_tasks。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from yai_core.learning.outcomes import extract_route_outcome
from yai_core.types import Strategy

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def bench():
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "benchmark_router", _SCRIPTS / "benchmark_router.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------- 任务集本身合法 ----------

def test_task_set_balanced_and_labeled(bench) -> None:
    from router_bench_tasks import TASKS

    assert len(TASKS) == 60
    for gt in ("direct", "react", "plan", "clarify"):
        assert sum(1 for t in TASKS if t.gt == gt) == 15
    assert all(t.task.strip() for t in TASKS)


# ---------- 仿真矩阵：正确策略成功、关键错配失败（reward 走真实抽取器） ----------

def _ok(bench, gt: Strategy, chosen: Strategy) -> bool:
    events, _ = bench.simulate(gt, chosen)
    return bool(
        extract_route_outcome(events, chosen, max_iters=6, clarify_budget=2).success
    )


def test_simulate_correct_strategies_succeed(bench) -> None:
    assert _ok(bench, Strategy.DIRECT, Strategy.DIRECT)
    assert _ok(bench, Strategy.REACT, Strategy.REACT)
    assert _ok(bench, Strategy.PLAN, Strategy.PLAN)
    assert _ok(bench, Strategy.CLARIFY, Strategy.CLARIFY)


def test_simulate_key_mismatches_fail(bench) -> None:
    # 该用工具却纯对话：模型只能说无能。
    assert not _ok(bench, Strategy.REACT, Strategy.DIRECT)
    assert not _ok(bench, Strategy.PLAN, Strategy.DIRECT)
    # 该规划却只 react：工具循环无法收敛，撞满迭代上限。
    assert not _ok(bench, Strategy.PLAN, Strategy.REACT)
    # 信息不全却硬执行：工具失败 / 硬答无能。
    assert not _ok(bench, Strategy.CLARIFY, Strategy.DIRECT)
    assert not _ok(bench, Strategy.CLARIFY, Strategy.REACT)
    assert not _ok(bench, Strategy.CLARIFY, Strategy.PLAN)
    # 清晰任务却被澄清：用户放弃。
    assert not _ok(bench, Strategy.REACT, Strategy.CLARIFY)
    assert not _ok(bench, Strategy.PLAN, Strategy.CLARIFY)


# ---------- 学习确实发生：五线关系成立 ----------

def test_bandit_beats_rules_and_ablations(bench) -> None:
    result = bench.aggregate(
        seeds=4, epochs=3, registry=bench.build_registry(), window=15
    )
    s = result["summary"]
    # 自校准显著优于不学习的规则基线。
    assert s["bandit"]["final_success"] > s["rules"]["final_success"] + 0.10
    # 完美路由是天花板。
    assert s["oracle"]["final_success"] >= s["bandit"]["final_success"]
    # 上下文特征是关键：无上下文的单 bandit 明显更差。
    assert s["bandit"]["final_success"] > s["ablation_no_context"]["final_success"] + 0.10
    # 规则先验有价值：带先验的完整 bandit 不劣于无先验消融（允许小幅噪声）。
    assert s["bandit"]["final_success"] >= s["ablation_no_prior"]["final_success"] - 0.03


def test_curves_complete_and_aligned(bench) -> None:
    result = bench.aggregate(
        seeds=2, epochs=2, registry=bench.build_registry(), window=15
    )
    assert set(result["curves"]) == set(bench.LINES)
    n = result["meta"]["steps"]
    assert n == 60 * 2
    for line in bench.LINES:
        curve = result["curves"][line]
        assert len(curve) == n
        assert all(0.0 <= v <= 1.0 for v in curve)


# ---------- 产物可渲染（标准库 SVG），且不依赖网络 ----------

def test_render_svg(bench, tmp_path) -> None:
    result = bench.aggregate(
        seeds=2, epochs=3, registry=bench.build_registry(), window=15
    )
    svg = tmp_path / "curve.svg"
    bench.render_svg(result["curves"], svg, epochs=3)
    text = svg.read_text(encoding="utf-8")
    assert text.lstrip().startswith("<svg")
    assert text.rstrip().endswith("</svg>")
    assert "自校准路由学习曲线" in text
    # 五条对比线各一条 polyline。
    assert text.count("<polyline") == len(bench.LINES)


def test_main_writes_outputs_and_passes_gate(bench, tmp_path) -> None:
    result = bench.main(seeds=4, epochs=3, out_dir=tmp_path)
    assert (tmp_path / "router_bench.json").exists()
    assert (tmp_path / "router_learning_curve.svg").exists()
    assert result["summary"]["bandit"]["final_success"] > result["summary"]["rules"][
        "final_success"
    ]
