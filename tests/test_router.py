import asyncio

import pytest

from yai_core import AdaptiveRouter, ModelResponse, Strategy, ToolRegistry, build_spec


def _registry_with_tools() -> ToolRegistry:
    reg = ToolRegistry()

    def search_notes(keyword: str) -> list:
        """搜索笔记。"""
        return []

    reg.register(build_spec(search_notes))
    return reg


class ScriptedClassifier:
    """测试用分类模型：返回预设内容，可记录调用并可模拟异常/超时。"""

    def __init__(self, content: str = "", *, raises: Exception | None = None,
                 sleep: float = 0.0) -> None:
        self._content = content
        self._raises = raises
        self._sleep = sleep
        self.calls = 0
        self.tiers: list[str] = []

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        self.tiers.append(tier)
        if self._raises is not None:
            raise self._raises
        if self._sleep:
            await asyncio.sleep(self._sleep)
        return ModelResponse(content=self._content)


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


# ---------- aclassify：LLM 分类 + 规则兜底 ----------

def test_aclassify_without_model_uses_rules() -> None:
    router = AdaptiveRouter()
    decision = asyncio.run(router.aclassify("搜索笔记", _registry_with_tools()))
    assert decision.source == "rules"
    assert decision.strategy == Strategy.REACT


def test_aclassify_llm_valid_decision() -> None:
    model = ScriptedClassifier(
        '{"strategy": "react", "tier": "standard", "reason": "需要调用搜索工具"}'
    )
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("帮我看看笔记里有啥", _registry_with_tools()))
    assert decision.source == "llm"
    assert decision.strategy == Strategy.REACT
    assert decision.tier == "standard"
    assert "搜索工具" in decision.reason
    assert model.calls == 1  # 只做一次轻量分类调用


def test_aclassify_llm_tolerates_fences_and_prose() -> None:
    model = ScriptedClassifier('好的，结果如下：\n```json\n{"strategy": "plan", '
                               '"tier": "strong", "reason": "多步任务"}\n```')
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("先查再汇总", _registry_with_tools()))
    assert decision.strategy == Strategy.PLAN and decision.tier == "strong"


def test_aclassify_llm_garbage_falls_back_to_rules() -> None:
    model = ScriptedClassifier("我不太确定你在说什么")
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("搜索笔记里的周报", _registry_with_tools()))
    assert decision.source == "rules"  # 解析失败 -> 规则兜底
    assert decision.strategy == Strategy.REACT
    assert "回退" in decision.reason


def test_aclassify_llm_exception_falls_back_to_rules() -> None:
    model = ScriptedClassifier(raises=RuntimeError("network down"))
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("搜索笔记", _registry_with_tools()))
    assert decision.source == "rules" and decision.strategy == Strategy.REACT


def test_aclassify_llm_timeout_falls_back_to_rules() -> None:
    model = ScriptedClassifier('{"strategy": "react"}', sleep=0.3)
    router = AdaptiveRouter(model=model, classify_timeout=0.05)
    decision = asyncio.run(router.aclassify("搜索笔记", _registry_with_tools()))
    assert decision.source == "rules" and decision.strategy == Strategy.REACT


def test_aclassify_llm_invalid_strategy_falls_back() -> None:
    model = ScriptedClassifier('{"strategy": "fly-to-moon", "reason": "瞎编"}')
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("搜索笔记", _registry_with_tools()))
    assert decision.source == "rules"


def test_aclassify_no_tools_skips_model_call() -> None:
    # 能力边界优先：宿主没工具时，连分类模型都不该调用。
    model = ScriptedClassifier('{"strategy": "react"}')
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("搜索一下", ToolRegistry()))
    assert decision.strategy == Strategy.DIRECT and model.calls == 0


def test_aclassify_empty_task_skips_model_call() -> None:
    model = ScriptedClassifier('{"strategy": "clarify"}')
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("   ", _registry_with_tools()))
    assert decision.strategy == Strategy.CLARIFY and model.calls == 0


def test_rules_plan_defaults_to_strong_tier() -> None:
    router = AdaptiveRouter()
    decision = asyncio.run(
        router.aclassify("先搜索订单，然后统计数量并整理成报告", _registry_with_tools())
    )
    assert decision.strategy == Strategy.PLAN and decision.tier == "strong"


@pytest.mark.parametrize("bad_tier", ["ultra", "", 3])
def test_aclassify_bad_tier_normalizes(bad_tier) -> None:
    model = ScriptedClassifier(f'{{"strategy": "react", "tier": "{bad_tier}"}}')
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("查笔记", _registry_with_tools()))
    assert decision.tier == "standard"


# ---------- Q2：数量/存在性问法必须进工具循环（"一共"不再是多步信号） ----------

@pytest.mark.parametrize(
    "task",
    ["我一共有多少条笔记？", "有没有关于周报的笔记", "我有几个待办", "今天几号"],
)
def test_quantifier_hints_go_react(task: str) -> None:
    # 有工具宿主：数量/存在性问法必须进工具循环，不能被误判 direct。
    router = AdaptiveRouter()
    assert router.classify(task, _registry_with_tools()) == Strategy.REACT


def test_yigong_alone_no_longer_triggers_plan() -> None:
    # "一共"是数量信号：无其他多步信号时不应判 plan（配合"多少"走 react）。
    router = AdaptiveRouter()
    decision = router._rules("我一共有多少条笔记", _registry_with_tools())
    assert decision.strategy == Strategy.REACT


# ---------- 能力缺口：missing_capability 字段 ----------

def test_llm_missing_capability_parsed() -> None:
    model = ScriptedClassifier(
        '{"strategy": "react", "tier": "standard", "reason": "需要天气", '
        '"missing_capability": "天气查询能力"}'
    )
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("查一下湛江天气", _registry_with_tools()))
    assert decision.missing_capability == "天气查询能力"


@pytest.mark.parametrize("raw", ["null", '""', '"  "', '"none"', '"NULL"'])
def test_llm_missing_capability_nullish_becomes_none(raw: str) -> None:
    model = ScriptedClassifier(
        f'{{"strategy": "react", "reason": "够用", "missing_capability": {raw}}}'
    )
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("搜索笔记", _registry_with_tools()))
    assert decision.missing_capability is None


def test_llm_missing_capability_absent_defaults_none() -> None:
    model = ScriptedClassifier('{"strategy": "react", "reason": "够用"}')
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("搜索笔记", _registry_with_tools()))
    assert decision.missing_capability is None


def test_llm_missing_capability_truncated() -> None:
    long_missing = "天气" * 100
    model = ScriptedClassifier(
        f'{{"strategy": "react", "reason": "缺能力", "missing_capability": "{long_missing}"}}'
    )
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("查天气", _registry_with_tools()))
    assert decision.missing_capability is not None
    assert len(decision.missing_capability) <= 120


def test_rules_never_produce_missing_capability() -> None:
    # 规则路径不具备能力缺口判断，恒为 None。
    router = AdaptiveRouter()
    for task in ["你好", "搜索笔记", "先查再汇总", "随便"]:
        decision = router._rules(task, _registry_with_tools())
        assert decision.missing_capability is None


def test_missing_capability_does_not_change_strategy() -> None:
    # 缺口感知只负责发事件，不改变本次执行策略。
    model = ScriptedClassifier(
        '{"strategy": "direct", "reason": "无工具可用", "missing_capability": "天气查询"}'
    )
    router = AdaptiveRouter(model=model)
    decision = asyncio.run(router.aclassify("查天气", _registry_with_tools()))
    assert decision.strategy == Strategy.DIRECT
    assert decision.missing_capability == "天气查询"


def test_classify_prompt_asks_missing_capability() -> None:
    # 分类契约必须显式包含 missing_capability 字段说明。
    import inspect

    source = inspect.getsource(AdaptiveRouter._llm_classify)
    assert "missing_capability" in source
