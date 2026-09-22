"""宿主 F 演示（语义通道）：缺口"换个说法"也能发现对的工具。

运行：.venv/Scripts/python.exe examples/host_f_discovery/run_semantic.py [可选任务文本]

与 run.py（关键词子串匹配）的区别：
- run.py 里缺口必须出现"天气"字样，关键词命中才能发现 get_weather；
- 本演示把缺口写成"出门该怎么穿、要不要带外套"——**一个"天气"字样都没有**，
  词法通道（含小型同义词表）召回失败；但接入 EmbeddingProvider 后，语义通道
  能理解"出门/外套/怎么穿"指向天气，仍然发现 get_weather。

全程离线、零成本、结果确定：这里用一个教学用的"概念轴向量" embedder
（ConceptEmbedder）模拟真实 embedding 服务；生产环境把它换成
``yai_core.integrations.embedding.OpenAICompatEmbedder``（接硅基/智谱/本地
Ollama 等 OpenAI 兼容 /embeddings 端点）即可，内核代码一行不改。

注意：Agnes / DeepSeek 的免费档实测不提供 embedding 端点（只有文本/图像/视频），
所以不配置 embedder 时语义通道自动关闭、退化为纯词法，不会报错、不花一分钱。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/

from runner_common import bootstrap, stream  # noqa: E402

bootstrap()

from host_f_discovery import capabilities as cap  # noqa: E402
from yai_core import (  # noqa: E402
    AgentCore,
    DiscoveredCandidate,
    ModelResponse,
    SemanticCatalog,
    ToolCallRequest,
    build_spec,
)

# 一个"天气"字样都没有的语义缺口。
DEFAULT_TASK = "帮我看看出门该怎么穿、要不要带外套"
SEMANTIC_NEED = "出门该怎么穿、要不要带外套"


class ConceptEmbedder:
    """教学用确定性 embedder：每个概念轴一个维度，命中该轴的词就在该维度置 1。

    真实 embedding 模型把文本编码成成百上千维稠密向量，余弦相似度能刻画"语义
    相近但字面不同"；这里用 3 个概念轴把这件事压缩到肉眼可验证的程度，且完全
    离线、可复现。
    """

    _AXES = (
        ("weather", ("天气", "带伞", "下雨", "气温", "外套", "出门", "wear", "伞")),
        ("stock", ("股票", "股价", "行情", "stock")),
        ("order", ("订单", "下单", "成交", "order")),
    )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vecs: list[list[float]] = []
        for text in texts:
            low = text.lower()
            vecs.append(
                [
                    1.0 if any(word in low for word in words) else 0.0
                    for _, words in self._AXES
                ]
            )
        return vecs


class SemanticGapModel:
    """离线确定性模型：报告一个语义缺口，再调用被语义发现的工具，最后收尾。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content=(
                    '{"strategy":"react","tier":"standard",'
                    '"reason":"需要穿衣/带伞建议，本质是天气，现有工具不足",'
                    f'"missing_capability":"{SEMANTIC_NEED}"}}'
                )
            )
        if self.calls == 2:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="w1", name="get_weather", arguments={"city": "湛江"}
                    )
                ],
            )
        return ModelResponse(content="湛江当前晴，27℃，穿短袖即可，出门不用带外套。")


def _candidates() -> list[DiscoveredCandidate]:
    return [
        DiscoveredCandidate(
            cap.get_weather,
            ("天气", "气温", "weather"),
            description="查询指定城市的实时天气（按需启用）",
        )
    ]


def _build_embedder(use_local: bool):
    """默认用离线教学概念轴（零依赖、CI 可跑）；--local 用真实本地 bge-m3。"""
    if not use_local:
        return ConceptEmbedder(), "离线教学概念轴（零 API Key，结果确定）"
    from yai_core.integrations.embedding import LocalBgeEmbedder  # noqa: PLC0415

    return LocalBgeEmbedder(), "本地 bge-m3 真实向量（CPU、离线、免费，需 .[local-embed]）"


async def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--local"]
    use_local = "--local" in sys.argv[1:]
    need = SEMANTIC_NEED

    embedder, embedder_label = _build_embedder(use_local)

    # 1) 同一份缺口，先看纯词法通道（无 embedder）能否召回——预期召不回。
    lexical = SemanticCatalog(_candidates())  # embedder=None
    lex_hits = await lexical.discover(need, task="", available=[])
    print("=" * 64)
    print("缺口描述：", need)
    print("（其中没有任何“天气”字样）")
    print("-" * 64)
    print("纯词法通道召回：", [s.name for s in lex_hits] or "（无）→ 关键词没命中")
    print("词法通道 last_error：", lexical.last_error)

    # 2) 接入语义通道后，同一个缺口能召回 get_weather。
    semantic = SemanticCatalog(_candidates(), embedder)
    sem_hits = await semantic.discover(need, task="", available=[])
    print("语义通道实现：", embedder_label)
    print("双通道（词法+语义）召回：", [s.name for s in sem_hits] or "（无）")
    print("=" * 64)
    print()

    # 3) 跑完整的"缺口 → 语义发现 → 注册 → 本轮调用"闭环。
    model = SemanticGapModel()
    core = AgentCore(model, llm_router=True, discovery=semantic)
    core.register_tools([build_spec(cap.list_tasks), build_spec(cap.add_task)])

    task = " ".join(args).strip() or DEFAULT_TASK
    await stream(core, task, embedder_label)


if __name__ == "__main__":
    asyncio.run(main())
