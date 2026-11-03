"""浏览器操作的治理声明：来源（Origin）边界、敏感字段与审批要求。

定位：Browser Provider 是**可选执行适配器**，它不自己发明一套权限系统——所有
工具仍注册进 ``ToolRegistry``、经 ``ToolExecutor`` 校验/审批/收据/事件；本模块
只提供浏览器特有的**额外收紧**（来源白名单、密码字段、审批声明生成）。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from yai_core.types import ApprovalPolicy, RateLimitHint


class BrowserError(RuntimeError):
    """浏览器适配层错误基类。"""


class BrowserPolicyError(BrowserError):
    """被浏览器来源 / 字段 / 能力策略拒绝。"""


class StaleElementRefError(BrowserError):
    """元素引用已过期（页面重渲染或已有更新的快照），必须重新观察。"""


class BrowserBackendError(BrowserError):
    """后端执行失败（页面关闭、导航失败等）。"""


def origin_of(url: str) -> str:
    """返回 ``scheme://host:port`` 小写来源；无法解析时返回空串。"""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def origin_allowed(url: str, allowed: frozenset[str]) -> bool:
    """判断 URL 是否在来源白名单内（空集合 = 宿主未限制，返回 True）。

    支持三种写法：精确来源 ``https://example.com``、通配 ``*``、以及
    ``https://*.example.com``（匹配子域，不再匹配裸域）。
    """
    if not allowed:
        return True
    origin = origin_of(url)
    if not origin:
        return False
    if "*" in allowed:
        return True
    if origin in allowed:
        return True
    host = _host_of(url)
    for pattern in allowed:
        if "://*." not in pattern:
            continue
        prefix, _, suffix = pattern.partition("://*.")
        if origin.startswith(prefix.lower() + "://") and host.endswith("." + suffix.lower()):
            return True
    return False


@dataclass(frozen=True)
class BrowserPolicy:
    """宿主对浏览器能力的约束声明（全部可选，默认宽松但可显式收紧）。

    Args:
        allowed_origins: 允许读取/操作的来源集合；空表示不额外限制。
        allow_password_fields: 是否允许向密码框输入（默认 False：拒绝）。
        allow_downloads: 是否允许下载（当前最小闭环不实现下载，保留声明位）。
        max_text_chars: 一次观察回传的页面文本上限（防止把整页灌进上下文）。
        max_elements: 一次观察回传的可交互元素上限。
        action_timeout_ms: 单次浏览器动作的工具级硬超时。
        require_approval_for: 需要人工审批的动作名集合（click / type / select /
            open / switch_tab）；空表示沿用内核默认策略。
        rate_limit_per_minute: 浏览器工具每分钟调用上限；None 表示不声明。
    """

    allowed_origins: frozenset[str] = frozenset()
    allow_password_fields: bool = False
    allow_downloads: bool = False
    max_text_chars: int = 8000
    max_elements: int = 60
    action_timeout_ms: int = 30_000
    require_approval_for: frozenset[str] = frozenset()
    rate_limit_per_minute: int | None = 60

    def __post_init__(self) -> None:
        for label, value in (
            ("max_text_chars", self.max_text_chars),
            ("max_elements", self.max_elements),
            ("action_timeout_ms", self.action_timeout_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"BrowserPolicy.{label} 必须是正整数")
        if self.rate_limit_per_minute is not None and (
            isinstance(self.rate_limit_per_minute, bool)
            or not isinstance(self.rate_limit_per_minute, int)
            or self.rate_limit_per_minute <= 0
        ):
            raise ValueError("BrowserPolicy.rate_limit_per_minute 必须是正整数或 None")

    def allows_origin(self, url: str) -> bool:
        return origin_allowed(url, self.allowed_origins)

    def check_origin(self, url: str, *, action: str = "observe") -> None:
        if not self.allows_origin(url):
            raise BrowserPolicyError(
                f"{action} 被来源策略拒绝：{origin_of(url) or url!r} 不在允许的来源内"
            )

    def approval_for(self, action: str) -> ApprovalPolicy | None:
        """按动作生成审批声明（数据形态，随能力清单可导出）。"""
        if action not in self.require_approval_for:
            return None
        return ApprovalPolicy(
            required=True,
            prompt=f"浏览器动作 {action} 需要人工确认",
        )

    def rate_limit(self) -> RateLimitHint | None:
        if self.rate_limit_per_minute is None:
            return None
        return RateLimitHint(count=self.rate_limit_per_minute, window="1m")
