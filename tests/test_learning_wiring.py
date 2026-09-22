"""M3 接线测试：自校准路由接入 router/core 热路径（默认关闭）。

覆盖：
- router 在"硬规则 -> 学习层 -> LLM -> 规则"优先级中的行为；
- 硬规则区域学习器不被询问；配置 LLM 路由时第一版学习器不前置；
- core.run() 结束后恰好回灌一次反馈（chosen/outcome 正确），on_feedback 被调用；
- 不传 learning 时行为不变；真实 ContextualBanditSelector 能接管并被观测。
全部离线，不需要 API Key。
"""

import asyncio
import json

from yai_core import (
    AdaptiveRouter,
    AgentCore,
    ModelResponse,
    Strategy,
    ToolRegistry,
    build_spec,
)
from yai_core.learning import (
    ContextualBanditSelector,
    RouteOutcome,
    RouteSuggestion,
)
from yai_core.spi import RouteSelector
from yai_core.types import EventType


def search_notes(keyword: str = "") -> list:
    """搜索笔记。"""
    return []


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(build_spec(search_notes))
    return reg


class FakeSelector:
    """脚本化学习器：记录调用，返回预设建议。"""

    def __init__(self, suggestion: RouteSuggestion | None = None) -> None:
        self._suggestion = suggestion
        self.suggest_calls: list[tuple[str, int]] = []
        self.records: list[tuple[str, Strategy, RouteOutcome]] = []

    def suggest(self, task: str, registry: ToolRegistry) -> RouteSuggestion | None:
        self.suggest_calls.append((task, len(registry)))
        return self._suggestion

    def record(self, task, registry, chosen, outcome) -> None:
        self.records.append((task, chosen, outcome))


class DirectModel:
    """一次性直接回答的离线模型（direct 路径成功收尾）。"""

    async def achat(self, messages, tools=None, *, tier="standard"):
        return ModelResponse(content="直接回答完成。")


class ScriptedClassifier:
    """返回固定路由 JSON 的分类模型。"""

    def __init__(self, content: str) -> None:
        self.content = content

    async def achat(self, messages, tools=None, *, tier="standard"):
        return ModelResponse(content=self.content)


# ---------- router 层：优先级 ----------

def test_selector_suggestion_used_without_llm() -> None:
    fake = FakeSelector(RouteSuggestion(Strategy.DIRECT, "bandit:thompson", 0.9))
    router = AdaptiveRouter(selector=fake)
    decision = asyncio.run(router.aclassify("查一下数据", _registry()))
    assert decision.strategy is Strategy.DIRECT
    assert decision.source == "bandit:thompson"
    assert len(fake.suggest_calls) == 1


def test_selector_silent_falls_back_to_rules() -> None:
    fake = FakeSelector(None)
    router = AdaptiveRouter(selector=fake)
    decision = asyncio.run(router.aclassify("查一下数据", _registry()))
    assert decision.strategy is Strategy.REACT
    assert decision.source == "rules"
    assert len(fake.suggest_calls) == 1  # 问过，但学习器不表态


def test_hard_rules_bypass_selector() -> None:
    # 即便学习器想给建议，空任务/无工具的确定性下限也不可越过。
    fake = FakeSelector(RouteSuggestion(Strategy.PLAN, "bandit:thompson", 0.9))
    router = AdaptiveRouter(selector=fake)
    empty = asyncio.run(router.aclassify("   ", _registry()))
    assert empty.strategy is Strategy.CLARIFY
    no_tools = asyncio.run(router.aclassify("查一下数据", ToolRegistry()))
    assert no_tools.strategy is Strategy.DIRECT
    assert fake.suggest_calls == []  # 硬规则区域根本没问学习器


def test_llm_router_takes_precedence_in_first_version() -> None:
    # 第一版：配置了 LLM 路由时仍以 LLM 为准，学习器不前置（只在后台积累反馈）。
    fake = FakeSelector(RouteSuggestion(Strategy.DIRECT, "bandit:thompson", 0.9))
    payload = json.dumps(
        {"strategy": "plan", "tier": "strong", "reason": "多步", "missing_capability": None}
    )
    router = AdaptiveRouter(model=ScriptedClassifier(payload), selector=fake)
    decision = asyncio.run(router.aclassify("查一下数据", _registry()))
    assert decision.source == "llm"
    assert decision.strategy is Strategy.PLAN
    assert fake.suggest_calls == []


def test_default_router_has_no_selector() -> None:
    assert AdaptiveRouter().selector is None


# ---------- core 层：反馈闭环 ----------

def test_core_records_feedback_once_after_run() -> None:
    fake = FakeSelector(RouteSuggestion(Strategy.DIRECT, "bandit:thompson", 0.9))
    hooks: list[tuple[str, Strategy, RouteOutcome]] = []
    core = AgentCore(
        DirectModel(),
        learning=fake,
        on_feedback=lambda task, chosen, outcome: hooks.append((task, chosen, outcome)),
    )
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下数据"))

    assert result.strategy is Strategy.DIRECT
    assert len(fake.records) == 1  # 恰好回灌一次
    task, chosen, outcome = fake.records[0]
    assert task == "查一下数据"
    assert chosen is Strategy.DIRECT
    assert outcome.success is True
    assert len(hooks) == 1  # 回调同样触发一次
    assert hooks[0][0] == "查一下数据"


def test_core_without_learning_is_unchanged() -> None:
    core = AgentCore(DirectModel())
    core.register_tools([build_spec(search_notes)])
    result = asyncio.run(core.run("你好"))
    assert result.strategy is Strategy.DIRECT
    assert core.route_learning_status() == {"enabled": False}


def test_real_bandit_takes_over_and_is_observable() -> None:
    # 强规则先验 + 关闭探索：规则偏好的 arm 几乎必然被 Thompson 选中，确定可复现。
    selector = ContextualBanditSelector(seed=0, epsilon=0.0, prior_strength=50.0)
    core = AgentCore(DirectModel(), learning=selector)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下数据"))

    sources = [
        e.data.get("source")
        for e in result.events
        if e.type is EventType.STRATEGY_SELECTED
    ]
    assert any(s and s.startswith("bandit:") for s in sources)
    status = core.route_learning_status()
    assert status["enabled"] is True
    assert status["selector"] == "ContextualBanditSelector"
    assert sum(status.get("obs", {}).values()) >= 1


def test_fake_selector_satisfies_spi() -> None:
    assert isinstance(FakeSelector(), RouteSelector)
    assert isinstance(ContextualBanditSelector(seed=0), RouteSelector)
