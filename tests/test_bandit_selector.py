"""ContextualBanditSelector 自校准路由测试（纯标准库、离线、确定性）。"""

import ast
import pathlib
import sys

import pytest

from yai_core.discovery import build_spec
from yai_core.learning import (
    ContextualBanditSelector,
    RouteOutcome,
    TaskFeatures,
)
from yai_core.tools.registry import ToolRegistry
from yai_core.types import Strategy


def _registry(n_tools: int = 3) -> ToolRegistry:
    reg = ToolRegistry()

    def _make(i: int):
        def fn() -> int:
            """测试工具。"""
            return i

        fn.__name__ = f"tool_{i}"
        return fn

    for i in range(n_tools):
        reg.register(build_spec(_make(i)))
    return reg


def _features(task: str, reg: ToolRegistry) -> TaskFeatures:
    return TaskFeatures.from_task(task, reg)


# ---------- 硬规则区域不表态 ----------

def test_empty_task_no_suggestion() -> None:
    sel = ContextualBanditSelector(seed=0)
    assert sel.suggest("   ", _registry()) is None


def test_no_tools_no_suggestion() -> None:
    sel = ContextualBanditSelector(seed=0)
    assert sel.suggest("搜索笔记", ToolRegistry()) is None


def test_cold_start_does_suggest_when_evidence_seeded() -> None:
    sel = ContextualBanditSelector(seed=0)
    sug = sel.suggest("搜索笔记", _registry())
    assert sug is not None and sug.source.startswith("bandit:")


# ---------- 规则先验 ----------

def test_cold_start_prior_prefers_rule_arm() -> None:
    reg = _registry()
    task = "先搜索客户，然后统计数量并整理成报告"
    sel = ContextualBanditSelector(seed=1, epsilon=0.0)
    sel.suggest(task, reg)  # 触发规则先验播种
    means = sel.arm_means(_features(task, reg))
    assert means["plan"] == max(means.values())  # 规则判 plan，plan arm 先验最强
    assert means["plan"] > means["react"]


# ---------- 学习收敛 ----------

def test_bandit_learns_rewarding_arm() -> None:
    reg = _registry()
    task = "查一下数据"
    sel = ContextualBanditSelector(seed=1, epsilon=0.0, prior_strength=0.0)
    win = RouteOutcome(True, 1.0)
    lose = RouteOutcome(False, 0.0)
    for _ in range(30):
        sel.record(task, reg, Strategy.DIRECT, lose)
        sel.record(task, reg, Strategy.REACT, win)
    means = sel.arm_means(_features(task, reg))
    assert means["react"] > means["direct"]
    assert means["react"] > 0.9 and means["direct"] < 0.1


def test_fractional_update() -> None:
    reg = _registry()
    task = "查一下数据"
    sel = ContextualBanditSelector(seed=0, prior_strength=0.0)
    sel.record(task, reg, Strategy.REACT, RouteOutcome(True, 0.5))
    means = sel.arm_means(_features(task, reg))
    assert means["react"] == pytest.approx(0.5, abs=1e-9)
    assert sel.observed_count(_features(task, reg)) == 1


# ---------- 探索 / 利用 ----------

def test_epsilon_one_explores() -> None:
    sel = ContextualBanditSelector(seed=2, epsilon=1.0)
    for _ in range(10):
        sug = sel.suggest("查一下数据", _registry())
        assert sug is not None and sug.source == "bandit:explore"


def test_epsilon_zero_exploits() -> None:
    sel = ContextualBanditSelector(seed=2, epsilon=0.0)
    for _ in range(10):
        sug = sel.suggest("查一下数据", _registry())
        assert sug is not None and sug.source == "bandit:thompson"


def test_min_samples_gate() -> None:
    # 关掉规则先验（均匀基线总 α+β=8），把门槛抬高到 100 -> 证据不足不表态。
    sel = ContextualBanditSelector(seed=0, prior_strength=0.0, min_samples=100.0)
    assert sel.suggest("查一下数据", _registry()) is None


# ---------- 确定性 ----------

def test_seed_is_reproducible() -> None:
    tasks = ["搜索笔记", "查一下订单", "先查客户再汇总", "今天几号", "统计数量", "列出待办"]
    reg = _registry()

    def run(seed: int) -> list[str]:
        sel = ContextualBanditSelector(seed=seed, epsilon=0.3)
        out = []
        for t in tasks:
            sug = sel.suggest(t, reg)
            out.append("none" if sug is None else sug.strategy.value)
        return out

    assert run(42) == run(42)
    assert run(42) != run(7)  # 不同种子采样序列不同（概率上）


# ---------- 持久化 ----------

def test_to_dict_from_dict_roundtrip() -> None:
    reg = _registry()
    task = "查一下数据"
    sel = ContextualBanditSelector(seed=3, epsilon=0.05, prior_strength=2.0)
    for _ in range(5):
        sel.record(task, reg, Strategy.REACT, RouteOutcome(True, 1.0))
    data = sel.to_dict()

    restored = ContextualBanditSelector.from_dict(data, seed=99)
    assert restored.epsilon == 0.05 and restored.prior_strength == 2.0
    assert restored.contexts() == sel.contexts()
    f = _features(task, reg)
    assert restored.arm_means(f) == sel.arm_means(f)
    assert restored.observed_count(f) == 5


def test_save_load_file_roundtrip(tmp_path) -> None:
    reg = _registry()
    task = "查一下数据"
    path = tmp_path / "bandit.json"
    sel = ContextualBanditSelector(seed=5)
    sel.record(task, reg, Strategy.PLAN, RouteOutcome(True, 0.8))
    sel.save(path)

    loaded = ContextualBanditSelector.load(path, seed=5)
    f = _features(task, reg)
    assert loaded.arm_means(f) == sel.arm_means(f)


# ---------- 零依赖红线 ----------

def test_learning_package_stdlib_only() -> None:
    import yai_core

    root = pathlib.Path(yai_core.__file__).parent / "learning"
    stdlib = set(sys.stdlib_module_names)
    for py in root.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [n.name for n in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            for mod in modules:
                top = mod.split(".")[0]
                assert top in stdlib or top == "yai_core", (
                    f"{py.name} 引入了非标准库依赖 {top!r}，违反内核零依赖红线"
                )
