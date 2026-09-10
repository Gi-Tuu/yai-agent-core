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


def test_explicit_mcp_intent_goes_react() -> None:
    # 显式要求用（MCP）工具提问，即使没有"查/搜"等动词也必须进工具循环，
    # 否则 direct 路径不传 tools，模型只能把工具调用写成文本（v0.2 实测回归）。
    router = AdaptiveRouter()
    task = "用 MCP 工具问一下 GitHub 仓库 python-sdk：Client 怎么初始化？"
    assert router.classify(task, _registry_with_tools()) == Strategy.REACT


def test_explicit_mcp_intent_case_insensitive() -> None:
    router = AdaptiveRouter()
    assert router.classify("调用 mcp 工具查询天气", _registry_with_tools()) == Strategy.REACT


def test_mcp_multistep_goes_plan() -> None:
    router = AdaptiveRouter()
    task = "先用 MCP 工具问一下这个仓库的结构，然后整理成一句话总结"
    assert router.classify(task, _registry_with_tools()) == Strategy.PLAN
