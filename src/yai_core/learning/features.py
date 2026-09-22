"""任务特征：把自然语言任务压成离散、可解释的上下文（context）。

自校准路由（contextual bandit）不直接读原始文本，而是读这组**标准库可算**的
特征。关键词词表直接复用 :mod:`yai_core.kernel.router`，不在两处各造一份，
避免规则与学习对同一任务的判断漂移。

第一版不引入 embedding（那是方向 B）；未来要接语义特征时，只需提供一个同样
产出 :class:`TaskFeatures` 的 featurizer，bandit 内核无需改动。
"""

from __future__ import annotations

from dataclasses import dataclass

# 复用规则路由的词表，保证"规则看到的信号"和"学习器看到的信号"一致。
from yai_core.kernel.router import _ACTION_HINTS, _CLARIFY_HINTS, _PLAN_HINTS
from yai_core.tools.registry import ToolRegistry

#: 长度分桶阈值（按 strip 后的字符数）：空 / 短 / 中 / 长。
_SHORT_MAX = 8
_MEDIUM_MAX = 30

#: 工具数分桶阈值：无 / 少量 / 大量。
_FEW_MAX = 8

#: 疑问信号（在行动/多步词表之外补充，用于识别"数量/存在性/疑问句"）。
_QUESTION_HINTS = (
    "？",
    "?",
    "多少",
    "几个",
    "几条",
    "几号",
    "有没有",
    "怎么",
    "如何",
    "为什么",
    "什么",
    "哪",
    "吗",
    "呢",
)


def _hit(text: str, lowered: str, hint: str) -> bool:
    """ASCII 词按小写匹配（英文/缩写），中文按原文匹配，与规则路由一致。"""
    return (hint in lowered) if hint.isascii() else (hint in text)


@dataclass(frozen=True)
class TaskFeatures:
    """一次任务的上下文特征。

    ``text`` 保留原始任务，仅供规则先验分类时调用规则路由，**不参与** :meth:`key`
    （否则每句话都是一个独立上下文，无法跨任务共享计数）。
    """

    text: str
    length_bucket: str  # empty / short / medium / long
    action: bool        # 命中行动词表（查/搜/统计/导出/调用工具…）
    multistep: bool     # 命中多步词表（先/然后/接着/汇总成…）
    vague: bool         # 命中模糊词表（随便/你看着办…）
    question: bool      # 疑问信号（问号/多少/有没有/怎么…）
    tools_bucket: str   # none / few / many

    @classmethod
    def from_task(cls, task: str, registry: ToolRegistry | None = None) -> TaskFeatures:
        text = (task or "").strip()
        if not text:
            length_bucket = "empty"
        elif len(text) <= _SHORT_MAX:
            length_bucket = "short"
        elif len(text) <= _MEDIUM_MAX:
            length_bucket = "medium"
        else:
            length_bucket = "long"

        lowered = text.lower()
        action = any(_hit(text, lowered, h) for h in _ACTION_HINTS)
        multistep = any(_hit(text, lowered, h) for h in _PLAN_HINTS)
        vague = any(_hit(text, lowered, h) for h in _CLARIFY_HINTS)
        question = any(_hit(text, lowered, h) for h in _QUESTION_HINTS)

        n_tools = len(registry) if registry is not None else 0
        if n_tools == 0:
            tools_bucket = "none"
        elif n_tools <= _FEW_MAX:
            tools_bucket = "few"
        else:
            tools_bucket = "many"

        return cls(
            text=text,
            length_bucket=length_bucket,
            action=action,
            multistep=multistep,
            vague=vague,
            question=question,
            tools_bucket=tools_bucket,
        )

    def key(self) -> tuple[str, bool, bool, bool, bool, str]:
        """bandit 状态表的上下文键。

        同类任务（长度档 × 行动/多步/模糊/疑问标志 × 工具规模）共享一组 Beta 后验，
        从而把"这次任务的成败"迁移到"同类任务"，而不是逐句记忆。
        """
        return (
            self.length_bucket,
            self.action,
            self.multistep,
            self.vague,
            self.question,
            self.tools_bucket,
        )
