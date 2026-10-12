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
    for key in (
        "memory_cipher",
        "context_compaction",
        "model_timeout",
        "sandbox",
        "memory_embedder",
        "auto_memory_recall",
        "auto_recall_k",
        "tool_result_char_limit",
    ):
        assert key in kwargs
    assert kwargs["tool_result_char_limit"] == 16_000
    assert kwargs["auto_memory_recall"] is False
    assert kwargs["auto_recall_k"] == 3


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
        ("auto_recall_k", 0),
        ("auto_recall_k", True),  # bool 不算条数
        ("tool_result_char_limit", 0),
        ("tool_result_char_limit", -1),
        ("tool_result_char_limit", True),  # bool 不算上界
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
    CoreConfig(tool_result_char_limit=None).validate()


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


class _OkayEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


class _BadEmbedder:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:  # 非 async
        return [[0.0] for _ in texts]


def test_auto_recall_requires_long_memory() -> None:
    with pytest.raises(ValueError):
        CoreConfig(auto_memory_recall=True, long_memory=False).validate()


def test_memory_embedder_must_be_async() -> None:
    with pytest.raises(ValueError):
        CoreConfig(memory_embedder=_BadEmbedder()).validate()


def test_valid_semantic_memory_config_passes() -> None:
    CoreConfig(
        long_memory=True,
        auto_memory_recall=True,
        auto_recall_k=5,
        memory_embedder=_OkayEmbedder(),
    ).validate()
