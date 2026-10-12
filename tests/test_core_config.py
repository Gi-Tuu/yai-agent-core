"""CoreConfig：集中默认值、早期校验与 AgentCore.from_config 的离线测试。"""

import asyncio

import pytest

from yai_core import AgentCore, Budget, CoreConfig, ModelResponse
from yai_core.kernel import CompactionPolicy


class ScriptedModel:
    def __init__(self, content: str = "ok") -> None:
        self.content = content
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        return ModelResponse(content=self.content)


def test_defaults_validate_and_kwargs_cover_keys() -> None:
    cfg = CoreConfig().validate()
    kwargs = cfg.as_kwargs()
    assert kwargs["max_iters"] == 6
    assert kwargs["llm_router"] is False
    assert kwargs["budget"] is None
    # 关键开关都应在展开字典里（供 from_config 使用）。
    for key in ("memory_cipher", "context_compaction", "model_timeout", "sandbox"):
        assert key in kwargs


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_iters", 0),
        ("max_iters", -3),
        ("max_iters", True),  # bool 不算次数
        ("max_clarify_rounds", -1),
        ("full_schema_budget", -1),
        ("llm_router", "maybe"),
        ("context_compaction", 1),
        ("budget", "not-a-budget"),
        ("model_timeout", 0),
        ("model_timeout", -2.5),
    ],
)
def test_invalid_values_rejected(field: str, value: object) -> None:
    cfg = CoreConfig(**{field: value})
    with pytest.raises(ValueError):
        cfg.validate()


def test_valid_budget_and_compaction_policy_pass() -> None:
    CoreConfig(budget=Budget(max_model_calls=3)).validate()
    CoreConfig(context_compaction=CompactionPolicy()).validate()
    CoreConfig(llm_router="auto").validate()


def test_agent_core_rejects_bad_kwargs_early() -> None:
    with pytest.raises(ValueError):
        AgentCore(ScriptedModel(), max_iters=0)


def test_from_config_matches_direct_construction() -> None:
    model = ScriptedModel("你好")
    cfg = CoreConfig(max_iters=3, llm_router=False)
    core = AgentCore.from_config(model, cfg)

    assert core._loop.max_iters == 3  # noqa: SLF001 - 验证配置透传到 loop
    result = asyncio.run(core.run("打个招呼"))
    assert result.final_text == "你好"


def test_from_config_validates_before_construction() -> None:
    with pytest.raises(ValueError):
        AgentCore.from_config(ScriptedModel(), CoreConfig(max_clarify_rounds=-1))
