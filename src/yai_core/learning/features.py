"""任务特征：把自然语言任务压成离散、可解释的上下文（context）。

自校准路由（contextual bandit）不直接读原始文本，而是读这组**标准库可算**的
特征。关键词词表直接复用 :mod:`yai_core.kernel.router`，不在两处各造一份，
避免规则与学习对同一任务的判断漂移。

第一版不引入 embedding（那是方向 B）；未来要接语义特征时，只需提供一个同样
产出 :class:`TaskFeatures` 的 featurizer，bandit 内核无需改动。
"""

from __future__ import annotations

import re
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
    "啥",
)

#: 学习器专用的多步连接词：比路由规则词表更宽，额外捕捉规则漏掉的单字
#: 连接（"并/再/分"）与收尾动词（"汇总/形成/分组"）。规则路由本身不变。
_MULTISTEP_EXTRA = (
    "并", "再", "最后", "形成", "汇总", "归类", "分组", "排序",
)
#: 数据 / 工具对象名词：围绕宿主数据的任务即使没有显性动词，也可能要工具
#: （"帮我看看上周的日记"、"List my pending orders"）。英文词小写匹配。
_DATA_OBJECT_HINTS = (
    "订单", "日记", "待办", "天气", "客户", "笔记", "资料", "纪要", "库存",
    "销售额", "报表", "数据", "记录", "联系方式", "业绩", "反馈", "简报", "周报",
    "清单", "金额", "心情", "备忘录",
    "notes", "note", "order", "orders", "weather", "customer", "todo", "pending",
)
#: 模糊指代 / 虚动词：信息不全、需要澄清，但未必命中"随便/你看着办"。
_UNSPECIFIED_HINTS = (
    "那个", "那边", "之前说", "相关的", "东西", "弄", "搞", "处理下", "跟进下",
    "看看吧", "安排安排",
)
#: 连续拉丁字母词（英文意图信号），如 Search / notes / orders。
_LATIN_WORD = re.compile(r"[a-z]{2,}")


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
    length_bucket: str       # empty / short / medium / long
    action: bool             # 命中行动词表（查/搜/统计/导出/调用工具…）
    multistep: bool          # 命中多步词表（含加宽的连接词）
    vague: bool              # 命中模糊词表（随便/你看着办…）
    question: bool           # 疑问信号（问号/多少/有没有/怎么/啥…）
    has_latin: bool          # 含连续拉丁字母词（英文意图）
    has_data_object: bool    # 含数据/工具对象名词（订单/日记/notes…）
    has_unspecified: bool    # 含模糊指代/虚动词（那个/弄/搞…）
    tools_bucket: str        # none / few / many

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
        multistep = any(
            _hit(text, lowered, h) for h in (*_PLAN_HINTS, *_MULTISTEP_EXTRA)
        )
        vague = any(_hit(text, lowered, h) for h in _CLARIFY_HINTS)
        question = any(_hit(text, lowered, h) for h in _QUESTION_HINTS)
        has_latin = bool(_LATIN_WORD.search(lowered))
        has_data_object = any(_hit(text, lowered, h) for h in _DATA_OBJECT_HINTS)
        has_unspecified = any(_hit(text, lowered, h) for h in _UNSPECIFIED_HINTS)

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
            has_latin=has_latin,
            has_data_object=has_data_object,
            has_unspecified=has_unspecified,
            tools_bucket=tools_bucket,
        )

    def key(self) -> tuple:
        """bandit 状态表的上下文键。

        同类任务（长度档 × 行动/多步/模糊/疑问/拉丁/数据对象/模糊指代标志 ×
        工具规模）共享一组 Beta 后验，从而把"这次任务的成败"迁移到"同类任务"，
        而不是逐句记忆。
        """
        return (
            self.length_bucket,
            self.action,
            self.multistep,
            self.vague,
            self.question,
            self.has_latin,
            self.has_data_object,
            self.has_unspecified,
            self.tools_bucket,
        )
