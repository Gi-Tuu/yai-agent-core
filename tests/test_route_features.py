"""TaskFeatures 任务特征抽取测试（纯标准库、离线）。"""

from yai_core.discovery import build_spec
from yai_core.learning import TaskFeatures
from yai_core.tools.registry import ToolRegistry


def _registry(n_tools: int) -> ToolRegistry:
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


# ---------- 长度分桶 ----------

def test_length_empty() -> None:
    assert TaskFeatures.from_task("   ", _registry(2)).length_bucket == "empty"


def test_length_buckets() -> None:
    assert TaskFeatures.from_task("你好", _registry(2)).length_bucket == "short"
    assert TaskFeatures.from_task("查" * 8, _registry(2)).length_bucket == "short"
    assert TaskFeatures.from_task("查" * 9, _registry(2)).length_bucket == "medium"
    assert TaskFeatures.from_task("查" * 30, _registry(2)).length_bucket == "medium"
    assert TaskFeatures.from_task("查" * 31, _registry(2)).length_bucket == "long"


# ---------- 词表信号 ----------

def test_action_signal() -> None:
    assert TaskFeatures.from_task("搜索笔记里的周报", _registry(2)).action is True
    assert TaskFeatures.from_task("你好呀", _registry(2)).action is False


def test_multistep_signal() -> None:
    f = TaskFeatures.from_task("先搜索客户，然后统计并整理成报告", _registry(2))
    assert f.multistep is True and f.action is True


def test_vague_signal() -> None:
    assert TaskFeatures.from_task("随便", _registry(2)).vague is True


def test_question_signal() -> None:
    assert TaskFeatures.from_task("我有多少条笔记？", _registry(2)).question is True
    assert TaskFeatures.from_task("有没有关于周报的", _registry(2)).question is True
    assert TaskFeatures.from_task("今天天气不错", _registry(2)).question is False


def test_english_ascii_lowercased() -> None:
    # 英文 MCP 关键词必须命中（规则路由的反例长尾之一）。
    f = TaskFeatures.from_task("call mcp tool to search notes", _registry(2))
    assert f.action is True


# ---------- 工具规模分桶 ----------

def test_tools_bucket() -> None:
    assert TaskFeatures.from_task("查", None).tools_bucket == "none"
    assert TaskFeatures.from_task("查", ToolRegistry()).tools_bucket == "none"
    assert TaskFeatures.from_task("查", _registry(1)).tools_bucket == "few"
    assert TaskFeatures.from_task("查", _registry(8)).tools_bucket == "few"
    assert TaskFeatures.from_task("查", _registry(9)).tools_bucket == "many"


# ---------- 上下文键 ----------

def test_key_shared_across_same_kind_tasks() -> None:
    reg = _registry(3)
    f1 = TaskFeatures.from_task("搜索笔记", reg)
    f2 = TaskFeatures.from_task("搜索订单", reg)
    assert f1.key() == f2.key()  # 同属"短/行动/有工具"，共享 bandit 计数


def test_key_differs_by_signal() -> None:
    reg = _registry(3)
    single = TaskFeatures.from_task("搜索笔记", reg)
    multi = TaskFeatures.from_task("先搜索笔记，然后统计并整理成报告", reg)
    assert single.key() != multi.key()


def test_text_not_in_key() -> None:
    f = TaskFeatures.from_task("搜索笔记", _registry(3))
    assert all(isinstance(part, (str, bool)) for part in f.key())
    assert "搜索笔记" not in f.key()
    assert f.text == "搜索笔记"  # 原文保留，供规则先验分类
