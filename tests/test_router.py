from yai_core import AdaptiveRouter, Strategy, ToolRegistry, build_spec


def _registry_with_tools() -> ToolRegistry:
    reg = ToolRegistry()

    def search_notes(keyword: str) -> list:
        """搜索笔记。"""
        return []

    reg.register(build_spec(search_notes))
    return reg


def test_no_tools_goes_direct() -> None:
    router = AdaptiveRouter()
    assert router.classify("你好", ToolRegistry()) == Strategy.DIRECT


def test_action_goes_react() -> None:
    router = AdaptiveRouter()
    assert router.classify("搜索笔记里关于周报的内容", _registry_with_tools()) == Strategy.REACT


def test_multistep_goes_plan() -> None:
    router = AdaptiveRouter()
    task = "先搜索订单，然后统计数量并整理成报告"
    assert router.classify(task, _registry_with_tools()) == Strategy.PLAN


def test_vague_goes_clarify() -> None:
    router = AdaptiveRouter()
    assert router.classify("随便", _registry_with_tools()) == Strategy.CLARIFY
