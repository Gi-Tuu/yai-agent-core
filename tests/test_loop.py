"""端到端测试：用脚本化假模型离线验证 react 循环，不需要 API Key。"""

import asyncio
import inspect
from collections.abc import AsyncGenerator, AsyncIterator
from typing import get_type_hints

import pytest

from yai_core import (
    AgentCore,
    AgentEvent,
    ChatMessage,
    ModelResponse,
    RouteDecision,
    SandboxResult,
    Strategy,
    ToolCallRequest,
    build_spec,
)
from yai_core.channels.collect import CollectChannel
from yai_core.kernel import AdaptiveRouter, AgentLoop, CompactionPolicy
from yai_core.kernel.loop import wrap_tool_output
from yai_core.kernel.meta_tools import MetaToolRuntime
from yai_core.learning import extract_route_outcome
from yai_core.memory import InMemoryStore
from yai_core.policy import AllowlistPolicy
from yai_core.tools import (
    CREATE_CODE_TOOL,
    REQUEST_CAPABILITY,
    ToolExecutor,
    ToolRegistry,
)


class ScriptedModel:
    """按预设顺序返回响应的假模型，用于离线测试。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


NOTES = {"周报": ["周一完成 Core 骨架", "周三跑通工具调用"], "其他": ["买菜"]}


def search_notes(keyword: str) -> list:
    """搜索宿主笔记库。"""
    return NOTES.get(keyword, [])


def test_react_loop_calls_tool_and_finishes() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="search_notes",
                                            arguments={"keyword": "周报"})],
            ),
            ModelResponse(content="本周周报包含 2 条记录：Core 骨架、工具调用。"),
        ]
    )
    core = AgentCore(model)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里关于周报的内容并总结"))

    event_types = [e.type.value for e in result.events]
    assert "strategy_selected" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert event_types[-1] == "done"
    assert "周报" in result.final_text
    assert model.calls == 2


def test_plan_strategy_still_executes_tools() -> None:
    """回归：plan 拆完步骤后必须真正进入工具循环，不能把计划当最终答复。"""

    def search_notes(keyword: str) -> list:
        """搜索宿主笔记库。"""
        return ["版本 0.22.1 已发布"] if keyword == "项目" else []

    model = ScriptedModel(
        [
            ModelResponse(content="1. 搜索项目笔记\n2. 统计总数并汇报"),  # 规划轮（无 tools）
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="p1", name="search_notes",
                                            arguments={"keyword": "项目"})],
            ),
            ModelResponse(content="找到 1 条项目相关笔记。"),
        ]
    )
    core = AgentCore(model)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("先搜索项目笔记，然后汇报"))

    assert result.strategy.value == "plan"
    event_types = [e.type.value for e in result.events]
    assert "plan_created" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "项目" in result.final_text


def test_direct_answer_without_tools() -> None:
    model = ScriptedModel([ModelResponse(content="你好，我是内嵌助手。")])
    core = AgentCore(model)
    result = asyncio.run(core.run("你好"))
    assert result.strategy.value == "direct"
    assert result.final_text.startswith("你好")


def test_llm_router_decision_flows_through_loop() -> None:
    """开启 llm_router 后：分类来源/理由/档位进事件，plan 用分类建议的 strong 档。"""

    class ClassifierThenPlan(ScriptedModel):
        async def achat(self, messages, tools=None, *, tier="standard"):
            self.tiers.append(tier)
            self.calls += 1
            # 第 1 次调用是路由分类（系统提示含"路由器"）
            if self.calls == 1:
                return ModelResponse(
                    content='{"strategy": "plan", "tier": "strong",'
                    ' "reason": "先拆解再执行"}'
                )
            # 第 2 次是规划轮
            if self.calls == 2:
                return ModelResponse(content="1. 搜索项目笔记\n2. 汇报结果")
            # 之后走父类的脚本队列（工具调用 + 最终答复）
            return self._responses[min(self.calls - 1, len(self._responses) - 1)]

        def __init__(self, responses) -> None:
            super().__init__(responses)
            self.tiers = []

    model = ClassifierThenPlan(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="p1", name="search_notes",
                                            arguments={"keyword": "项目"})],
            ),
            ModelResponse(content="找到 1 条项目笔记。"),
        ]
    )
    core = AgentCore(model, llm_router=True)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("先搜索项目笔记，然后汇报"))

    selected = next(e for e in result.events if e.type.value == "strategy_selected")
    assert selected.data["source"] == "llm"
    assert selected.data["strategy"] == "plan"
    assert selected.data["tier"] == "strong"
    # 分类用 standard（轻量快），规划轮按建议用 strong，工具循环回 standard
    assert model.tiers[0] == "standard"
    assert model.tiers[1] == "strong"
    assert result.final_text.startswith("找到")


# ---------- llm_router="auto"：按 provider 能力标记决定是否 LLM 分类 ----------

class _LiveLikeModel(ScriptedModel):
    """模拟真实联网后端：自报 yai_live_router 标记。"""

    yai_live_router = True


def test_auto_uses_rules_for_offline_model() -> None:
    # auto + 离线脚本模型：不做分类调用，走规则路由（离线确定性）。
    model = ScriptedModel([ModelResponse(content="离线结果")])
    core = AgentCore(model, llm_router="auto")
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    assert result.events[0].data["source"] == "rules"
    assert model.calls == 1  # 只有执行阶段的一次调用，无分类调用


def test_auto_uses_llm_for_live_model() -> None:
    # auto + 自报联网的后端：第一次调用用于分类。
    model = _LiveLikeModel(
        [
            ModelResponse(content='{"strategy":"react","tier":"standard","reason":"用搜索"}'),
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="c1", name="search_notes", arguments={"keyword": "周报"}
                )],
            ),
            ModelResponse(content="完成"),
        ]
    )
    core = AgentCore(model, llm_router="auto")
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("帮我看看笔记里有啥"))

    assert result.events[0].data["source"] == "llm"
    assert model.calls >= 2


def test_llm_router_bool_true_still_uses_llm_without_marker() -> None:
    # 显式 True 不依赖标记：即使模型没有 yai_live_router 也走 LLM 分类（向后兼容）。
    model = ScriptedModel(
        [
            ModelResponse(content='{"strategy":"react","tier":"standard","reason":"用搜索"}'),
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="c1", name="search_notes", arguments={"keyword": "周报"}
                )],
            ),
            ModelResponse(content="完成"),
        ]
    )
    core = AgentCore(model, llm_router=True)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("帮我看看笔记里有啥"))

    assert result.events[0].data["source"] == "llm"


def test_llm_router_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="llm_router"):
        AgentCore(ScriptedModel([]), llm_router="yes")  # type: ignore[arg-type]


# ---------- 能力缺口感知：capability_missing 事件 ----------

def test_loop_emits_capability_missing_event() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content='{"strategy":"react","tier":"standard","reason":"缺天气",'
                '"missing_capability":"天气查询能力"}'
            ),
            ModelResponse(content="我当前没有天气查询能力，无法回答。"),
        ]
    )
    core = AgentCore(model, llm_router=True)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下湛江天气"))

    events = [e for e in result.events if e.type.value == "capability_missing"]
    assert len(events) == 1
    data = events[0].data
    assert data["missing"] == "天气查询能力"
    assert data["strategy"] == "react"
    assert "search_notes" in data["available_tools"]
    assert "建议" not in data  # 事件只描述缺口，不给宿主规定动作


def test_loop_no_capability_missing_when_tools_sufficient() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content='{"strategy":"react","tier":"standard","reason":"用搜索",'
                '"missing_capability":null}'
            ),
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="c1", name="search_notes", arguments={"keyword": "周报"}
                )],
            ),
            ModelResponse(content="完成"),
        ]
    )
    core = AgentCore(model, llm_router=True)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的周报并总结"))

    assert not [e for e in result.events if e.type.value == "capability_missing"]


def test_loop_no_capability_missing_on_rules_path() -> None:
    # 规则路径不具备能力缺口判断，不发事件。
    model = ScriptedModel([ModelResponse(content="离线结果")])
    core = AgentCore(model)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    assert not [e for e in result.events if e.type.value == "capability_missing"]


def test_loop_no_capability_missing_on_clarify() -> None:
    # 即使模型在 clarify 决策里误填 missing_capability，也不发事件（Q4 边界）。
    model = ScriptedModel(
        [
            ModelResponse(
                content='{"strategy":"clarify","tier":"standard","reason":"信息不足",'
                '"missing_capability":"某能力"}'
            ),
            ModelResponse(content="好的"),
        ]
    )
    channel = ScriptedChannel([""])  # 空回答 -> 体面收尾
    core = AgentCore(model, llm_router=True, channel=channel)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("随便弄一下"))

    assert not [e for e in result.events if e.type.value == "capability_missing"]


def test_loop_no_capability_missing_without_tools() -> None:
    # 无工具部署形态：分类短路走规则 direct，不发缺口事件。
    model = ScriptedModel(
        [
            ModelResponse(
                content='{"strategy":"direct","reason":"闲聊","missing_capability":"天气"}'
            ),
            ModelResponse(content="你好"),
        ]
    )
    core = AgentCore(model, llm_router=True)

    result = asyncio.run(core.run("你好"))

    assert not [e for e in result.events if e.type.value == "capability_missing"]


# ---------- 澄清（clarify）循环：不允许无限反问 ----------

class ScriptedChannel(CollectChannel):
    """按队列返回澄清回答的通道，并记录被问到的问题。"""

    def __init__(self, answers: list[str]) -> None:
        super().__init__()
        self._answers = list(answers)
        self.questions: list[str] = []

    async def ask(self, question: str) -> str:
        self.questions.append(question)
        return self._answers.pop(0) if self._answers else ""


class AlwaysClarifyRouter:
    """测试桩：无论任务文本都判 clarify，用于强制打满澄清轮数。"""

    def __init__(self) -> None:
        self.calls = 0

    async def aclassify(self, task, registry, **_kwargs):
        self.calls += 1
        return RouteDecision(Strategy.CLARIFY, "llm", "测试桩：始终判澄清")

    def classify(self, task, registry):
        return Strategy.CLARIFY


def test_clarify_question_mentions_original_task_and_converges() -> None:
    model = ScriptedModel([ModelResponse(content="好的，这是结果。")])
    channel = ScriptedChannel(["请直接回答我就行"])
    core = AgentCore(model, channel=channel)

    result = asyncio.run(core.run("随便"))

    assert len(channel.questions) == 1
    assert "随便" in channel.questions[0]
    assert result.events[-1].type.value == "done"
    assert result.final_text.startswith("好的")
    assert model.calls == 1
    assert [e.type.value for e in result.events].count("done") == 1


def test_clarify_accumulates_context_across_rounds() -> None:
    """原任务必须与补充信息累积后重新路由，不能丢上下文乒乓反问。"""
    model = ScriptedModel([ModelResponse(content="已按补充信息处理。")])
    # 第一轮仍模糊（短句 <12 字命中规则），第二轮给足信息
    channel = ScriptedChannel(["随便", "请直接回答我就行"])
    core = AgentCore(model, channel=channel)

    result = asyncio.run(core.run("随便"))

    assert len(channel.questions) == 2
    # 第二轮反问带着原任务和第一轮补充，而不是只拿回答重新路由
    assert channel.questions[1].count("随便") >= 2
    assert "补充信息" in channel.questions[1]
    assert result.events[-1].type.value == "done"


def test_clarify_rounds_capped_then_falls_back_to_direct() -> None:
    model = ScriptedModel([ModelResponse(content="信息仍不足，请补充关键对象。")])
    channel = ScriptedChannel(["继续模糊", "还是模糊"])
    core = AgentCore(model, channel=channel, router=AlwaysClarifyRouter())

    result = asyncio.run(core.run("随便"))

    selected = [e for e in result.events if e.type.value == "strategy_selected"]
    assert selected[0].data["strategy"] == "clarify"
    assert selected[-1].data["strategy"] == "direct"  # 达上限降级，不再追问
    assert len(channel.questions) == 2  # 只问 2 轮
    rounds = [e.data["round"] for e in result.events
              if e.type.value == "clarify_requested"]
    assert rounds == [1, 2]  # 轮次从 1 递增
    assert selected[-1].data["tier"] == selected[0].data["tier"]  # tier 不被无声丢弃
    assert result.events[-1].type.value == "done"
    assert [e.type.value for e in result.events].count("done") == 1
    assert result.strategy.value == "direct"


def test_clarify_question_truncates_long_task_and_uses_generic_wording() -> None:
    long_task = "随便" + "甲乙丙丁" * 60  # 242 字
    model = ScriptedModel([])
    channel = ScriptedChannel([""])
    core = AgentCore(model, channel=channel, router=AlwaysClarifyRouter())

    result = asyncio.run(core.run(long_task))

    question = next(
        e.data["question"] for e in result.events if e.type.value == "clarify_requested"
    )
    assert "…" in question
    assert "哪个对象或哪件事" in question  # 通用话术，领域名词只作举例
    assert len(question) < len(long_task)


def test_clarify_empty_answer_aborts_gracefully() -> None:
    model = ScriptedModel([])
    channel = ScriptedChannel([""])  # 用户放弃 / 超时 / 终止
    core = AgentCore(model, channel=channel)

    result = asyncio.run(core.run("随便"))

    assert result.events[-1].type.value == "done"
    assert [e.type.value for e in result.events].count("done") == 1
    assert "已停止" in result.final_text
    assert model.calls == 0  # 空回答直接收尾，不调用模型


# ======================================================================
# 边界与防御路径补测：react 异常 / 迭代上限 / plan 解析 / 发现去重 /
# 两层目录聚焦 / meta-tool 授权与降级。全部离线，不需要 API Key。
# ======================================================================

def get_weather(city: str) -> str:
    """查询某城市天气。"""
    return f"{city} 晴"


def list_tasks() -> list:
    """列出待办任务。"""
    return ["写作业"]


class RaisingModel:
    """每次调用都抛异常的假模型。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        raise RuntimeError("模型服务不可用")


def test_react_model_raises_emits_error_and_stops() -> None:
    core = AgentCore(RaisingModel())
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    errors = [e for e in result.events if e.type.value == "error"]
    assert errors
    assert "RuntimeError" in errors[0].data.get("error", "")
    # 出错即停：只有一次模型调用，done 仍收尾。
    assert core.model.calls == 1
    assert result.events[-1].type.value == "done"


@pytest.mark.parametrize("max_iters", [1, 2, 6])
def test_iteration_limit_finishes_with_existing_observations(max_iters: int) -> None:
    tool_resp = ModelResponse(
        content="",
        tool_calls=[ToolCallRequest(id="t", name="search_notes",
                                    arguments={"keyword": "周报"})],
    )
    model = ScriptedModel(
        [tool_resp] * max_iters + [ModelResponse(content="基于已有结果给出答案。")]
    )
    core = AgentCore(model, max_iters=max_iters)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    assert model.calls == max_iters + 1  # 工具轮 + 1 轮上限收尾
    assert result.events[-1].data["model_calls"] == model.calls
    assert result.final_text == "基于已有结果给出答案。"
    messages = [event for event in result.events if event.type.value == "model_message"]
    assert len(messages) == 1
    assert messages[0].data == {
        "text": result.final_text,
        "tier": "standard",
        "iteration_limit": True,
        "max_iters": max_iters,
        "reason": "iteration_limit",
    }
    assert core.memory.history()[-1].content == result.final_text


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_iteration_limit_failure_keeps_decision_markers(failure: str) -> None:
    requests: list[dict] = []
    cancelled: list[bool] = []

    class FailingFinish(ScriptedModel):
        async def achat(self, messages, tools=None, *, tier="standard"):
            requests.append({"messages": messages, "tools": tools})
            if self.calls == 0:
                return await super().achat(messages, tools, tier=tier)
            self.calls += 1
            if failure == "timeout":
                try:
                    await asyncio.Future()
                finally:
                    cancelled.append(True)
            raise RuntimeError("private finalization failure")

    model = FailingFinish([ModelResponse(tool_calls=[ToolCallRequest(
        id="t", name="search_notes", arguments={"keyword": "周报"},
    )])])
    core = AgentCore(model, max_iters=1, model_timeout=0.05 if failure == "timeout" else None)
    core.register_tools([build_spec(search_notes)])
    result = asyncio.run(core.run("搜索笔记里的周报"))

    assert model.calls == result.events[-1].data["model_calls"] == 2
    assert requests[-1]["tools"] is None
    assert requests[-1]["messages"][-1] == {
        "role": "user", "content": "已达到工具调用上限，请基于已有结果直接给出最终答案。",
    }
    assert any(message["role"] == "tool" for message in requests[-1]["messages"])
    messages = [event for event in result.events if event.type.value == "model_message"]
    assert len(messages) == 1
    assert messages[0].data == {
        "text": "",  # 失败仍为空答案，不额外生成文案污染上下文/历史
        "tier": "standard",
        "iteration_limit": True,
        "max_iters": 1,
        "reason": "iteration_limit",
    }
    errors = [event for event in result.events if event.type.value == "error"]
    assert len(errors) == 1
    assert errors[0].data["stage"] == "model"
    assert result.events[-1].type.value == "done"
    assert result.final_text == core.memory.history()[-1].content == messages[0].data["text"]
    assert not extract_route_outcome(result.events, result.strategy, max_iters=1).success
    assert cancelled == ([True] if failure == "timeout" else [])


def test_plan_with_unparseable_steps_falls_back_to_task() -> None:
    model = ScriptedModel(
        [
            ModelResponse(content="好的，我这就处理。"),  # 规划轮：无编号 → steps=[task]
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="p", name="search_notes",
                                            arguments={"keyword": "周报"})],
            ),
            ModelResponse(content="完成。"),
        ]
    )
    core = AgentCore(model)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("先搜索笔记里的周报，然后汇报"))

    plan = next(e for e in result.events if e.type.value == "plan_created")
    assert plan.data["steps"] == ["先搜索笔记里的周报，然后汇报"]
    assert result.final_text == "完成。"


# ---------- Fake 发现源 / 沙箱 ----------

class FakeDiscovery:
    """脚本化发现源：可控制 discover 返回、抛错及 list_discoverable 行为。"""

    def __init__(
        self,
        *,
        specs=None,
        discoverable=None,
        discover_raises=None,
        list_raises=None,
    ) -> None:
        self._specs = specs or []
        self._discoverable = discoverable
        self._discover_raises = discover_raises
        self._list_raises = list_raises
        self.calls: list[str] = []

    async def discover(self, need, *, task=None, available=None):
        self.calls.append(need)
        if self._discover_raises is not None:
            raise self._discover_raises
        return self._specs

    def list_discoverable(self):
        if self._list_raises is not None:
            raise self._list_raises
        return self._discoverable


class FakeSandbox:
    """总是成功、原样回传入参的假沙箱（仅供创建/接线测试）。"""

    async def execute(self, code, inputs, *, timeout=None):
        return SandboxResult(ok=True, output=inputs, error=None)


def test_discovery_dedups_registered_and_duplicate_candidates() -> None:
    weather_spec = build_spec(get_weather)
    model = ScriptedModel(
        [
            ModelResponse(
                content='{"strategy":"react","tier":"standard","reason":"缺天气",'
                '"missing_capability":"天气查询能力"}'
            ),
            ModelResponse(content="我现在能查天气了。"),
        ]
    )
    # 候选：search_notes（宿主已注册）、weather 重复两次。
    discovery = FakeDiscovery(
        specs=[build_spec(search_notes), weather_spec, weather_spec]
    )
    core = AgentCore(model, llm_router=True, discovery=discovery)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下天气"))

    discovered = next(e for e in result.events if e.type.value == "tool_discovered")
    assert discovered.data["registered"] == [weather_spec.name]


def test_discoverable_catalog_list_raises_degrades_silently() -> None:
    model = ScriptedModel([ModelResponse(content="结果。")])
    discovery = FakeDiscovery(list_raises=RuntimeError("目录服务挂了"))
    core = AgentCore(model, discovery=discovery)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    assert result.events[-1].type.value == "done"


# ---------- 两层目录：超预算时的工具聚焦 ----------

class FocusRaiseThenAnswer(ScriptedModel):
    """聚焦调用抛错，之后走父类脚本（验证聚焦失败回退全量）。"""

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("聚焦服务失败")
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]


def test_focus_raises_falls_back_to_full_schema() -> None:
    model = FocusRaiseThenAnswer([ModelResponse(content="最终答案。")])
    core = AgentCore(model, full_schema_budget=1)
    core.register_tools([build_spec(search_notes), build_spec(get_weather)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    assert result.final_text == "最终答案。"


def test_focus_invalid_and_duplicate_names_skipped() -> None:
    model = ScriptedModel(
        [
            ModelResponse(content='["search_notes","ghost","search_notes"]'),  # 聚焦
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c", name="search_notes",
                                            arguments={"keyword": "周报"})],
            ),
            ModelResponse(content="找到周报。"),
        ]
    )
    core = AgentCore(model, full_schema_budget=1)
    core.register_tools([build_spec(search_notes), build_spec(get_weather)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    assert result.final_text.startswith("找到")


def test_focus_returns_invalid_json_falls_back() -> None:
    model = ScriptedModel(
        [
            ModelResponse(content="我觉得是 [abc] 这样"),  # 聚焦：[abc] 非法 JSON
            ModelResponse(content="最终答案。"),
        ]
    )
    core = AgentCore(model, full_schema_budget=1)
    core.register_tools([build_spec(search_notes), build_spec(get_weather)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    assert result.final_text == "最终答案。"


def test_focused_schema_appends_newly_registered_tool() -> None:
    schemas: list[list[dict] | None] = []

    class CaptureSchemas(ScriptedModel):
        async def achat(self, messages, tools=None, *, tier="standard"):
            schemas.append(tools)
            return await super().achat(messages, tools, tier=tier)

    model = CaptureSchemas(
        [
            ModelResponse(content='["search_notes"]'),  # 聚焦
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="r", name=REQUEST_CAPABILITY,
                    arguments={"need": "任务清单能力"})],
            ),
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="l", name="list_tasks",
                                            arguments={})],
            ),
            ModelResponse(content="任务：写作业。"),
        ]
    )
    discovery = FakeDiscovery(specs=[build_spec(list_tasks)])
    core = AgentCore(model, full_schema_budget=1, discovery=discovery)
    core.register_tools([build_spec(search_notes), build_spec(get_weather)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    assert "写作业" in result.final_text
    assert model.calls == result.events[-1].data["model_calls"] == 4
    focused = [event for event in result.events if event.data.get("kind") == "schema_focus"]
    assert len(focused) == 1
    assert focused[0].data["schema_focus"] == {
        "mode": "focused", "reason": "selected",
        "selected": ["search_notes", REQUEST_CAPABILITY], "available_count": 3,
    }  # 发现新工具不能反向改写已发事件的 selected 快照
    assert schemas[1] is not None and schemas[2] is not None
    assert {schema["function"]["name"] for schema in schemas[1]} == {
        "search_notes", REQUEST_CAPABILITY,
    }
    assert {schema["function"]["name"] for schema in schemas[2]} == {
        "search_notes", REQUEST_CAPABILITY, "list_tasks",
    }


# ---------- meta-tool：request_capability 缺 need ----------

def test_request_capability_missing_need_replies_guidance() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="r", name=REQUEST_CAPABILITY,
                                            arguments={})],
            ),
            ModelResponse(content="我无法获得该能力。"),
        ]
    )
    core = AgentCore(model, discovery=FakeDiscovery())
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    # need 为空：在发缺口事件前就回灌，故无 capability_missing / tool_discovered。
    assert not [e for e in result.events if e.type.value == "capability_missing"]
    assert not [e for e in result.events if e.type.value == "tool_discovered"]
    assert model.calls == 2


# ---------- 直接构造 AgentLoop：覆盖经 Core 不可达的防御分支 ----------

def _bare_loop(model, *, meta_tools=(), discovery=None, register=None):
    registry = ToolRegistry()
    policy = AllowlistPolicy(mode="allow_all")
    channel = CollectChannel()
    # 无沙箱、无 code_manager 的裸执行器。
    executor = ToolExecutor(registry, policy, channel)
    if register:
        registry.register_many(register)
    return AgentLoop(
        model, registry, executor, channel, InMemoryStore(),
        router=AdaptiveRouter(), discovery=discovery, meta_tools=meta_tools,
    )


async def _collect_events(agen) -> list:
    out: list = []
    async for ev in agen:
        out.append(ev)
    return out


def test_request_capability_without_discovery_replies() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="r", name=REQUEST_CAPABILITY,
                                            arguments={"need": "天气能力"})],
            ),
            ModelResponse(content="能力不足。"),
        ]
    )
    loop = _bare_loop(
        model, meta_tools={REQUEST_CAPABILITY}, register=[build_spec(search_notes)]
    )

    events = asyncio.run(_collect_events(loop.astream("搜索笔记里的周报")))

    cap = [e for e in events if e.type.value == "capability_missing"]
    assert cap and cap[0].data.get("phase") == "react"
    assert not [e for e in events if e.type.value == "tool_discovered"]
    assert model.calls == 2


def test_create_code_tool_without_sandbox_replies() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="c", name=CREATE_CODE_TOOL,
                    arguments={"name": "mytool", "description": "d",
                               "code": "out = 1"})],
            ),
            ModelResponse(content="无法创建。"),
        ]
    )
    loop = _bare_loop(
        model, meta_tools={CREATE_CODE_TOOL}, register=[build_spec(search_notes)]
    )

    events = asyncio.run(_collect_events(loop.astream("搜索笔记里的周报")))

    assert not [e for e in events if e.type.value == "code_tool_created"]
    assert model.calls == 2


def test_unknown_meta_tool_defensive_reply() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="x", name="bogus_meta",
                                            arguments={})],
            ),
            ModelResponse(content="好的。"),
        ]
    )
    loop = _bare_loop(
        model, meta_tools={"bogus_meta"}, register=[build_spec(search_notes)]
    )

    events = asyncio.run(_collect_events(loop.astream("搜索笔记里的周报")))

    assert model.calls == 2
    assert events[-1].type.value == "done"


def test_meta_runtime_contract_and_discovery_iterator_signature() -> None:
    assert inspect.isabstract(MetaToolRuntime)
    assert MetaToolRuntime.__abstractmethods__ == {
        "_model_messages", "_model_chat", "_discover_tools",
    }
    assert not inspect.isabstract(AgentLoop)
    assert inspect.iscoroutinefunction(MetaToolRuntime._model_messages)
    assert inspect.iscoroutinefunction(MetaToolRuntime._model_chat)
    assert not inspect.iscoroutinefunction(MetaToolRuntime._discover_tools)
    assert inspect.isasyncgenfunction(AgentLoop._discover_tools)
    assert get_type_hints(AgentLoop.astream)["return"] == AsyncGenerator[AgentEvent, None]


def test_discover_tools_none_returns_early() -> None:
    loop: AgentLoop = _bare_loop(ScriptedModel([]), register=[build_spec(search_notes)])

    async def go() -> list[AgentEvent]:
        iterator: AsyncIterator[AgentEvent] = loop._discover_tools("天气", "查天气")
        assert isinstance(iterator, AsyncIterator)
        assert not inspect.isawaitable(iterator)
        return [event async for event in iterator]

    assert asyncio.run(go()) == []


# ---------- create_code_tool：input_schema 兜底 / 创建校验 ----------

def test_create_code_tool_non_dict_schema_uses_default() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="c", name=CREATE_CODE_TOOL,
                    arguments={"name": "score_tool", "description": "打分",
                               "code": "out = {'s': 1}",
                               "input_schema": "not-a-dict"})],
            ),
            ModelResponse(content="已创建，打分完成。"),
        ]
    )
    core = AgentCore(model, sandbox=FakeSandbox())
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("计算打分"))

    created = next(e for e in result.events if e.type.value == "code_tool_created")
    assert created.data["name"] == "score_tool"
    spec = core.registry.get("score_tool")
    assert spec.input_schema == {"type": "object", "properties": {}}
    assert "已创建" in result.final_text


def test_create_code_tool_missing_code_raises_valueerror() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="c", name=CREATE_CODE_TOOL,
                    arguments={"name": "bad_tool", "description": "desc",
                               "code": ""})],
            ),
            ModelResponse(content="创建失败，改用现有工具。"),
        ]
    )
    core = AgentCore(model, sandbox=FakeSandbox())
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("计算打分"))

    assert not [e for e in result.events if e.type.value == "code_tool_created"]
    assert not core.registry.has("bad_tool")
    assert model.calls == 2


# ======================================================================
# 第二轮：把剩余未覆盖分支补齐——可发现目录构建 / 发现源异常 / 聚焦回退 /
# 长历史回放与真压缩 / 工具失败反思 / meta-tool 授权拒绝与空匹配。
# ======================================================================

class DiscoverOnly:
    """只有 discover、没有 list_discoverable 方法的发现源。"""

    async def discover(self, need, *, task=None, available=None):
        return []


def failing_lookup(keyword: str) -> str:
    """总是失败的查询工具。"""
    raise RuntimeError("数据库连接失败")


def test_discoverable_catalog_built_when_list_returns_items() -> None:
    discovery = FakeDiscovery(
        discoverable=[
            {"name": "get_weather", "summary": "查询天气"},
            {"name": "search_notes", "summary": "已注册，应被过滤"},
            {"name": "get_weather", "summary": "重复项"},
        ]
    )
    model = ScriptedModel([ModelResponse(content="结果。")])
    core = AgentCore(model, discovery=discovery)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    assert result.events[-1].type.value == "done"


def test_discoverable_without_list_method_returns_none() -> None:
    model = ScriptedModel([ModelResponse(content="结果。")])
    core = AgentCore(model, discovery=DiscoverOnly())
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    assert result.events[-1].type.value == "done"


def test_discovery_discover_raises_emits_error() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content='{"strategy":"react","tier":"standard","reason":"缺",'
                '"missing_capability":"天气能力"}'
            ),
            ModelResponse(content="暂时无法查天气。"),
        ]
    )
    discovery = FakeDiscovery(discover_raises=RuntimeError("发现源宕机"))
    core = AgentCore(model, llm_router=True, discovery=discovery)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下天气"))

    errors = [e for e in result.events if e.type.value == "error"]
    assert errors and errors[0].data.get("stage") == "tool_discovery"


def test_discovery_no_new_specs_returns_silently() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content='{"strategy":"react","tier":"standard","reason":"缺",'
                '"missing_capability":"x能力"}'
            ),
            ModelResponse(content="好的。"),
        ]
    )
    discovery = FakeDiscovery(specs=[build_spec(search_notes)])  # 全部已注册
    core = AgentCore(model, llm_router=True, discovery=discovery)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("查一下天气"))

    assert not [e for e in result.events if e.type.value == "tool_discovered"]


def test_focus_no_brackets_falls_back() -> None:
    model = ScriptedModel(
        [
            ModelResponse(content="我不确定该用哪个工具"),  # 聚焦：无方括号
            ModelResponse(content="最终答案。"),
        ]
    )
    core = AgentCore(model, full_schema_budget=1)
    core.register_tools([build_spec(search_notes), build_spec(get_weather)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    assert result.final_text == "最终答案。"


async def _seed_long_history(memory) -> None:
    for i in range(3):
        await memory.append_history(
            ChatMessage(role="user", content=f"第{i}轮问题" + "长" * 80)
        )
        await memory.append_history(
            ChatMessage(role="assistant", content=f"第{i}轮回答" + "长" * 80)
        )


def test_long_history_replayed_and_compacted() -> None:
    memory = InMemoryStore()
    asyncio.run(_seed_long_history(memory))
    policy = CompactionPolicy(
        hard_max_chars=300, target_chars=150, keep_recent_rounds=1
    )
    model = ScriptedModel(
        [
            ModelResponse(content="早期对话的摘要。"),  # compact 摘要调用
            ModelResponse(content="压缩后最终答复。"),  # react 主调用
        ]
    )
    core = AgentCore(model, memory=memory, context_compaction=policy)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    compacted = [e for e in result.events if e.type.value == "context_compacted"]
    assert compacted
    assert result.final_text == "压缩后最终答复。"


def test_failed_tool_appends_reflection() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="f", name="failing_lookup",
                    arguments={"keyword": "周报"})],
            ),
            ModelResponse(content="数据库暂不可用，无法完成。"),
        ]
    )
    core = AgentCore(model)
    core.register_tools([build_spec(failing_lookup)])

    result = asyncio.run(core.run("查询周报数据"))

    assert model.calls == 2
    assert "不可用" in result.final_text


def test_request_capability_denied_replies() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="r", name=REQUEST_CAPABILITY,
                    arguments={"need": "天气能力"})],
            ),
            ModelResponse(content="我无法获得该能力。"),
        ]
    )
    core = AgentCore(
        model, policy=AllowlistPolicy(mode="deny_all"),
        discovery=FakeDiscovery(),
    )
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    # 授权被拒：在发缺口事件前就回灌，故无 capability_missing。
    assert not [e for e in result.events if e.type.value == "capability_missing"]
    assert model.calls == 2


def test_create_code_tool_denied_replies() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="c", name=CREATE_CODE_TOOL,
                    arguments={"name": "t", "description": "d",
                               "code": "out = 1"})],
            ),
            ModelResponse(content="无法创建，改用现有工具。"),
        ]
    )
    core = AgentCore(
        model, policy=AllowlistPolicy(mode="deny_all"),
        sandbox=FakeSandbox(),
    )
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("计算打分"))

    assert not [e for e in result.events if e.type.value == "code_tool_created"]
    assert model.calls == 2


def test_request_capability_no_match_reply() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="r", name=REQUEST_CAPABILITY,
                    arguments={"need": "超能力"})],
            ),
            ModelResponse(content="宿主确实没有超能力。"),
        ]
    )
    core = AgentCore(model, discovery=FakeDiscovery(specs=[]))
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的周报"))

    cap = [e for e in result.events if e.type.value == "capability_missing"]
    assert cap and cap[0].data.get("phase") == "react"
    assert not [e for e in result.events if e.type.value == "tool_discovered"]
    assert model.calls == 2


def test_tool_output_is_wrapped_as_untrusted_data() -> None:
    """工具返回值回灌给模型时必须包成不可信数据块（Spotlighting 最小实现）。"""
    captured: dict[str, str] = {}

    class _Capture(ScriptedModel):
        async def achat(self, messages, tools=None, *, tier="standard"):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_calls=[ToolCallRequest(id="c1", name="echo", arguments={})]
                )
            tool_msg = next(
                m for m in reversed(messages) if m.get("role") == "tool"
            )
            captured["content"] = tool_msg["content"]
            return ModelResponse(content="ok")

    async def echo() -> str:
        """返回一句话。"""
        return "结果是 42"

    core = AgentCore(_Capture([]), llm_router=False)
    core.register_tools([build_spec(echo)])
    asyncio.run(core.run("调用 echo 工具"))

    # 执行器把工具返回值序列化成 JSON 文本，所以原文带引号。
    raw = '"结果是 42"'
    content = captured["content"]
    assert content.startswith('<tool_output untrusted="true">')
    assert raw in content
    assert content.rstrip().endswith("</tool_output>")
    assert content == wrap_tool_output(raw)


def test_tool_output_guard_can_be_disabled() -> None:
    """宿主可以关掉包裹，模型看到的就是裸返回值。"""
    captured: dict[str, str] = {}

    class _Capture(ScriptedModel):
        async def achat(self, messages, tools=None, *, tier="standard"):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_calls=[ToolCallRequest(id="c1", name="echo", arguments={})]
                )
            captured["content"] = next(
                m for m in reversed(messages) if m.get("role") == "tool"
            )["content"]
            return ModelResponse(content="ok")

    async def echo() -> str:
        """返回一句话。"""
        return "结果是 42"

    core = AgentCore(_Capture([]), llm_router=False, tool_output_guard=False)
    core.register_tools([build_spec(echo)])
    asyncio.run(core.run("调用 echo 工具"))
    assert captured["content"] == '"结果是 42"'
