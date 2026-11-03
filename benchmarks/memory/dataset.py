"""小型离线记忆评测集（v0.26 Batch 6）：结构化记忆 + 标注 query + 假嵌入器。

为什么自带假嵌入器：CI 不允许依赖模型文件或网络。假嵌入器按"概念轴"生成确定性
向量，可以证明**语义通道独立于字面重叠**（跨语言改写也能命中）；但它**不代表**
真实 BGE-M3 的效果——真实语义效果需要本地模型另行验证（本仓库 `models/` 下没有
模型文件时，脚本会明确跳过并标注）。
"""

from __future__ import annotations

from typing import Any

#: 概念轴：命中任一触发词该维度置 1（中英混排，用于构造"词法零重叠、语义相关"）。
AXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dark", ("深色", "暗色", "dark", "夜间", "night")),
    ("allergy", ("芒果", "mango", "过敏", "allergy")),
    ("finance", ("记账", "账本", "预算", "budget", "expense", "账单")),
    ("travel", ("出差", "旅行", "trip", "travel", "航班", "flight")),
    ("pet", ("猫", "cat", "宠物", "pet", "狗", "dog")),
    ("work", ("周报", "weekly", "report", "汇报", "日报")),
    ("contact", ("电话", "phone", "邮箱", "email", "联系方式")),
    ("food", ("饮食", "忌口", "food", "diet", "素食", "vegetarian")),
    ("music", ("音乐", "music", "歌单", "playlist")),
)

def _note(key: str, content: str, kind: str, *tags: str) -> dict[str, Any]:
    return {"key": key, "content": content, "kind": kind, "tags": list(tags)}


#: 记忆条目；key 是评测用稳定标识（运行时用正文反查 id）。
NOTES: tuple[dict[str, Any], ...] = (
    _note("dark_pref", "用户偏好深色主题界面", "preference", "主题"),
    _note("mango_allergy", "用户对芒果严重过敏", "fact", "过敏"),
    _note("weekly_report", "每周五需要提交周报给组长", "fact", "工作"),
    _note("budget_tool", "用户用记账 App 管理每月预算", "preference", "记账"),
    _note("travel_seat", "出差订票时优先靠窗座位", "preference", "出差"),
    _note("cat_name", "用户养了一只叫团子的猫", "profile", "宠物"),
    _note("phone_number", "用户的工作电话是 13800000000", "fact", "联系方式"),
    _note("vegetarian", "用户是素食主义者", "preference", "饮食"),
    _note("playlist", "用户喜欢在写代码时听 lo-fi 歌单", "preference", "音乐"),
    _note("night_mode", "夜间模式字体要调大一号", "note", "主题", "可访问性"),
    _note("expense_app", "每月月底用预算工具核对支出", "episode", "记账"),
    _note("flight_alert", "航班延误时要改签", "note", "出差"),
    _note("pet_vet", "团子每年要去宠物医院打疫苗", "fact", "宠物"),
    _note("email_pref", "用户更愿意用邮件而不是电话沟通", "preference", "联系方式"),
    _note("diet_note", "过敏体质需要看饮食忌口清单", "note", "饮食", "过敏"),
    _note("music_work", "工作时听歌会戴降噪耳机", "note", "音乐"),
    _note("report_deadline", "周报截止时间是周五下午六点", "fact", "工作"),
    _note("dark_theme_code", "深色模式在代码编辑器里最舒服", "preference", "主题"),
)


def _q(question: str, *expected: str) -> dict[str, Any]:
    return {"q": question, "expected": tuple(expected)}


#: 标注 query：expected 为该 query 的相关记忆 key 集合（可多选）。
QUERIES: tuple[dict[str, Any], ...] = (
    _q("深色主题", "dark_pref", "night_mode", "dark_theme_code"),
    _q("dark mode preference", "dark_pref", "night_mode"),
    _q("芒果过敏", "mango_allergy", "diet_note"),
    _q("mango allergy", "mango_allergy"),
    _q("周报什么时候交", "weekly_report", "report_deadline"),
    _q("记账预算", "budget_tool", "expense_app"),
    _q("出差订票", "travel_seat", "flight_alert"),
    _q("宠物猫", "cat_name", "pet_vet"),
    _q("联系方式", "phone_number", "email_pref"),
    _q("素食", "vegetarian"),
    _q("歌单", "playlist"),
    _q("budget tool", "budget_tool"),
)


class ConceptEmbedder:
    """确定性假嵌入器：按概念轴生成 0/1 向量（仅用于证明通道接线，不代表真实语义）。"""

    def __init__(self) -> None:
        self.calls = 0

    def _vector(self, text: str) -> list[float]:
        lowered = text.lower()
        return [
            1.0 if any(trigger in lowered for trigger in triggers) else 0.0
            for _, triggers in AXES
        ]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vector(text) for text in texts]


class BrokenEmbedder:
    """恒失败的嵌入器：验证语义通道失败时降级纯词法。"""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise RuntimeError("embedding backend down")
