"""浏览器操作的治理声明：来源（Origin）边界、敏感字段与审批要求。

定位：Browser Provider 是**可选执行适配器**，它不自己发明一套权限系统——所有
工具仍注册进 ``ToolRegistry``、经 ``ToolExecutor`` 校验/审批/收据/事件；本模块
只提供浏览器特有的**额外收紧**（来源白名单、密码字段、审批声明生成）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from yai_core.types import ApprovalPolicy, RateLimitHint

#: 安全默认只允许的 Web 协议；file:/javascript:/data:/about: 等需要显式放开。
SUPPORTED_SCHEMES = frozenset({"http", "https"})

#: 会改变页面状态、生产安全预设默认要求审批的浏览器动作。
MUTATING_ACTIONS = frozenset({"click", "type", "select", "open", "switch_tab"})


class BrowserSecurityWarning(UserWarning):
    """使用了宽松默认策略（开发/测试模式）时的显式告警。"""


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
    #: 是否允许 file:/javascript:/data:/about: 等非 HTTP(S) 协议。
    #: 默认 False：这些协议不在来源白名单语义内，也最容易绕过来源边界。
    allow_special_schemes: bool = False
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
        for origin in self.allowed_origins:
            if origin == "*":
                continue
            if "://*." in origin:
                scheme, _, host = origin.partition("://*.")
            else:
                parts = urlsplit(origin)
                scheme, host = parts.scheme.lower(), parts.netloc
            if scheme not in SUPPORTED_SCHEMES or not host:
                raise ValueError(
                    "BrowserPolicy.allowed_origins 只接受 http(s) 来源或 "
                    f"https://*.host 通配，得到 {origin!r}"
                )

    @classmethod
    def secure(
        cls,
        allowed_origins: Any,
        *,
        allow_downloads: bool = False,
        rate_limit_per_minute: int | None = 60,
        max_text_chars: int = 8000,
        max_elements: int = 60,
        action_timeout_ms: int = 30_000,
    ) -> BrowserPolicy:
        """生产安全预设：必须显式列出来源，修改性动作默认要审批。

        与默认构造的区别：``allowed_origins`` 不能为空、不能是 ``*``；click /
        type / select / open / switch_tab 默认进审批；密码框、下载、特殊协议关闭。
        """
        if isinstance(allowed_origins, str):
            allowed_origins = [allowed_origins]
        origins = frozenset(
            str(item).strip().rstrip("/").lower() for item in allowed_origins
        )
        if not origins:
            raise ValueError("BrowserPolicy.secure 必须显式提供至少一个允许来源")
        if "*" in origins:
            raise ValueError("BrowserPolicy.secure 不接受全通配 '*'; 请显式列出来源")
        return cls(
            allowed_origins=origins,
            allow_password_fields=False,
            allow_downloads=allow_downloads,
            allow_special_schemes=False,
            max_text_chars=max_text_chars,
            max_elements=max_elements,
            action_timeout_ms=action_timeout_ms,
            require_approval_for=MUTATING_ACTIONS,
            rate_limit_per_minute=rate_limit_per_minute,
        )

    @classmethod
    def development(cls) -> BrowserPolicy:
        """显式的开发/测试宽松预设；生产请用 :meth:`secure`。"""
        return cls()

    @property
    def origins_unrestricted(self) -> bool:
        """来源白名单是否为空或含 ``*``——即来源闸不构成安全边界。

        这是**安全告警**的口径：只要来源不受限就值得提醒，与是否配置了审批无关。
        """
        return not self.allowed_origins or "*" in self.allowed_origins

    @property
    def is_permissive(self) -> bool:
        """是否处于“来源全开 **且** 未配置额外审批”的宽松模式。

        与 :attr:`origins_unrestricted` 的区别：只配了审批、没配来源白名单时本属性为
        False，但来源其实仍未受限——所以 ``BrowserProvider`` 的告警只认
        :attr:`origins_unrestricted`，不认本属性。
        """
        return self.origins_unrestricted and not self.require_approval_for

    def allows_origin(self, url: str) -> bool:
        try:
            scheme = (urlsplit((url or "").strip()).scheme or "").lower()
        except ValueError:
            return False
        if scheme not in SUPPORTED_SCHEMES and not self.allow_special_schemes:
            return False
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
