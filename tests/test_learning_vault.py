"""shell 路由自学习状态读写/摘要测试：不依赖 Qt / 网络 / Key。

learning_vault.py 只用 yai_core 与标准库；shell/desktop/__init__.py 会连带
导入 PySide6，故按文件路径直接加载（与 test_tool_vault.py 同模式），
CI 精简矩阵（无 Qt）也能真实执行。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_VAULT = ROOT / "shell" / "desktop" / "learning_vault.py"


def _load_vault():
    spec = importlib.util.spec_from_file_location("yai_learning_vault", _VAULT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _registry():
    from yai_core import ToolRegistry, discover

    class _Host:
        def get_value(self, key: str) -> str:
            """按键取值。"""
            return key

    reg = ToolRegistry()
    reg.register_many(discover(_Host()))
    return reg


def _feed(mod, path, task="帮我查一下这个 key 的值", reward=0.95):
    """跑一次 suggest + record 并落盘，返回选择器。"""
    from yai_core import Strategy
    from yai_core.learning import RouteOutcome

    reg = _registry()
    selector = mod.load_selector(path)
    selector.suggest(task, reg)
    selector.record(task, reg, Strategy.REACT,
                    RouteOutcome(success=True, reward=reward))
    mod.save_selector(selector, path)
    return selector


def test_describe_context_renders_labels() -> None:
    mod = _load_vault()
    # length, action, multistep, vague, question, has_latin, data_obj, unspec, tools
    parts = ["medium", True, False, False, False, False, False, False, "few"]
    label = mod.describe_context(parts)
    assert "中等任务" in label and "单步行动" in label and "工具少" in label


def test_load_cold_start_when_missing(tmp_path) -> None:
    mod = _load_vault()
    selector = mod.load_selector(tmp_path / "nope.json")
    assert selector.__class__.__name__ == "ContextualBanditSelector"
    assert selector.contexts() == []


def test_load_tolerates_corrupt_file(tmp_path) -> None:
    mod = _load_vault()
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    assert mod.load_selector(p).contexts() == []


def test_save_load_roundtrip_keeps_obs(tmp_path) -> None:
    mod = _load_vault()
    path = tmp_path / "learning.json"
    _feed(mod, path)
    # 重新从磁盘加载：真实观测数保留。
    data = mod.load_selector(path).to_dict()
    assert sum(data["obs"].values()) == 1


def test_summarize_disabled_when_missing(tmp_path) -> None:
    mod = _load_vault()
    status = mod.summarize(tmp_path / "nope.json")
    assert status["enabled"] is False and status["learned_tasks"] == 0
    assert status["buckets"] == []


def test_summarize_tolerates_corrupt_file(tmp_path) -> None:
    mod = _load_vault()
    p = tmp_path / "bad.json"
    p.write_text("corrupt!!!", encoding="utf-8")
    assert mod.summarize(p)["enabled"] is False


def test_summarize_reports_learned_buckets(tmp_path) -> None:
    mod = _load_vault()
    path = tmp_path / "learning.json"
    _feed(mod, path)
    status = mod.summarize(path)
    assert status["enabled"] is True
    assert status["learned_tasks"] == 1
    assert status["context_buckets"] == 1
    bucket = status["buckets"][0]
    assert bucket["observed"] == 1
    assert bucket["preferred_label"]
    assert set(bucket["means"]) == {"direct", "react", "plan", "clarify"}
