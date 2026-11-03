"""浏览器观察模型：页面快照、元素引用与标签页（v0.26 Browser Provider）。

这一层只定义**与具体浏览器无关**的数据形状，不 import Playwright，也不碰内核
其它模块。真实执行由宿主或 ``playwright_backend`` 提供，测试用假后端。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ElementInfo:
    """快照里的一个可交互元素。

    ``backend_ref`` 是**后端不透明句柄**（Playwright 侧是当页临时属性，扩展侧是
    节点键），只用于回传给后端执行；对模型暴露的是带快照版本的公开引用。
    """

    backend_ref: str
    role: str = ""
    name: str = ""
    tag: str = ""
    input_type: str = ""
    value: str = ""
    disabled: bool = False
    sensitive: bool = False
    frame_id: str = "main"

    def to_public(self, ref: str) -> dict[str, Any]:
        """给模型看的元素描述；密码等敏感字段不回显值。"""
        out: dict[str, Any] = {"ref": ref}
        if self.role:
            out["role"] = self.role
        if self.name:
            out["name"] = self.name[:200]
        if self.tag:
            out["tag"] = self.tag
        if self.input_type:
            out["input_type"] = self.input_type
        if self.disabled:
            out["disabled"] = True
        if self.sensitive:
            out["sensitive"] = True
        elif self.value:
            out["value"] = self.value[:200]
        if self.frame_id != "main":
            out["frame_id"] = self.frame_id
        return out


@dataclass(frozen=True)
class TabInfo:
    tab_id: str
    url: str = ""
    title: str = ""
    active: bool = False

    def to_public(self) -> dict[str, Any]:
        return {
            "tab_id": self.tab_id,
            "url": self.url,
            "title": self.title[:200],
            "active": self.active,
        }


@dataclass(frozen=True)
class PageObservation:
    """一次页面观察结果（后端返回的原始快照）。"""

    url: str
    title: str = ""
    text: str = ""
    elements: tuple[ElementInfo, ...] = ()
    tabs: tuple[TabInfo, ...] = ()
    tab_id: str = ""
    frame_id: str = "main"
    #: 页面状态指纹（URL / 标题 / DOM 文本的确定性摘要），用于判断操作是否产生
    #: 可观察变化；为空表示后端不支持变化检测（此时结果一律记为 unknown）。
    fingerprint: str = ""

    def with_display(self) -> PageObservation:
        return self

    def element_by_index(self, index: int) -> ElementInfo | None:
        if 0 <= index < len(self.elements):
            return self.elements[index]
        return None


@dataclass
class Snapshot:
    """一次观察在 Provider 内的登记：版本号 + 公开 ref → 元素。"""

    version: int
    observation: PageObservation
    elements: dict[str, ElementInfo] = field(default_factory=dict)

    @property
    def snapshot_id(self) -> str:
        return f"s{self.version}"
