"""Windows 桌面操作的治理声明与安全边界（v0.26 Desktop Provider）。

与 Browser Provider 同一原则：桌面适配器不发明第二套权限系统，所有工具仍走
``ToolRegistry`` + ``ToolExecutor`` 的权限 / 审批 / 收据 / 事件；本模块只提供
平台特有的**额外收紧**：

- 进程白名单（只允许操作宿主声明的进程）；
- 高风险控件名（删除 / 卸载 / 格式化 / 关机 …）默认拒绝，宿主显式放行才执行；
- 几何坐标点击（``click``）默认关闭，只能作为显式兜底；
- 焦点漂移保护：物理输入前必须确认目标窗口在前台；
- 用户接管：``pause()`` 后所有动作拒绝，直到 ``resume()``；
- UAC / 安全桌面不可跨（适配器如实报错，不尝试绕过）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yai_core.types import ApprovalPolicy, RateLimitHint


class DesktopSecurityWarning(UserWarning):
    """使用了宽松默认策略（开发/测试模式）时的显式告警。"""

#: 默认视为高风险的控件名称关键词（中文 + 英文，保守匹配）。
HIGH_RISK_KEYWORDS = (
    "删除",
    "卸载",
    "格式化",
    "关机",
    "重启",
    "重置",
    "清空",
    "delete",
    "remove",
    "uninstall",
    "format",
    "shutdown",
    "restart",
    "reset",
    "wipe",
)


class DesktopError(RuntimeError):
    """桌面适配层错误基类。"""


class DesktopPolicyError(DesktopError):
    """被来源 / 进程 / 高风险 / 接管状态拒绝。"""


class StaleControlRefError(DesktopError):
    """控件引用已过期（窗口状态变化或已有更新快照），必须重新观察。"""


class DesktopBackendError(DesktopError):
    """后端执行失败（窗口关闭、UIA 不可用等）。"""


@dataclass(frozen=True)
class DesktopPolicy:
    """宿主的桌面能力约束声明。

    Args:
        allowed_processes: 允许操作的**精确 PID** 集合（不做前缀匹配）。
        allowed_process_names: 允许的进程映像名（如 ``notepad.exe``，大小写不敏感）。
        allowed_process_paths: 允许的进程完整路径（大小写不敏感）。
        allowed_window_titles: 允许操作的窗口标题子串集合；仅标题约束不构成完整进程身份。
        allow_high_risk_controls: 是否允许操作名称命中风险关键词的控件（默认 False）。
        allow_geometry_click: 是否允许坐标兜底点击（默认 False，只允许语义 UIA 操作）。
        max_controls: 一次观察回传的控件上限。
        action_timeout_ms: 单次动作的工具级硬超时。
        require_approval_for: 需要人工审批的动作（invoke / set_value / click / focus）。
        rate_limit_per_minute: 桌面工具每分钟调用上限。
    """

    allowed_processes: frozenset[str] = frozenset()
    allowed_process_names: frozenset[str] = frozenset()
    allowed_process_paths: frozenset[str] = frozenset()
    allowed_window_titles: frozenset[str] = frozenset()
    allow_high_risk_controls: bool = False
    allow_geometry_click: bool = False
    max_controls: int = 80
    action_timeout_ms: int = 15_000
    require_approval_for: frozenset[str] = frozenset({"invoke", "set_value", "select", "click"})
    rate_limit_per_minute: int | None = 120

    def __post_init__(self) -> None:
        for label, value in (
            ("max_controls", self.max_controls),
            ("action_timeout_ms", self.action_timeout_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"DesktopPolicy.{label} 必须是正整数")
        if self.rate_limit_per_minute is not None and (
            isinstance(self.rate_limit_per_minute, bool)
            or not isinstance(self.rate_limit_per_minute, int)
            or self.rate_limit_per_minute <= 0
        ):
            raise ValueError("DesktopPolicy.rate_limit_per_minute 必须是正整数或 None")

    @classmethod
    def secure(
        cls,
        allowed_processes: Any = (),
        *,
        allowed_process_names: Any = (),
        allowed_process_paths: Any = (),
        allowed_window_titles: Any = (),
    ) -> DesktopPolicy:
        """生产安全预设：必须显式声明允许的进程（可再叠加窗口标题子串）。

        高风险控件、坐标点击保持关闭；invoke/set_value/click 默认要审批。
        控件名称关键词不是安全边界（本地化/无名称/图标按钮可绕过），宿主必须
        用进程/标题白名单把可操作范围收窄。
        """
        def _set(value: Any) -> frozenset[str]:
            if isinstance(value, str):
                value = [value]
            return frozenset(str(item).strip() for item in value if str(item).strip())

        processes = _set(allowed_processes)
        names = frozenset(item.casefold() for item in _set(allowed_process_names))
        paths = frozenset(
            item.replace("/", "\\").casefold() for item in _set(allowed_process_paths)
        )
        if not (processes or names or paths):
            raise ValueError(
                "DesktopPolicy.secure 必须提供至少一个进程身份（PID / 映像名 / 路径）"
            )
        titles = _set(allowed_window_titles)
        return cls(
            allowed_processes=processes,
            allowed_process_names=names,
            allowed_process_paths=paths,
            allowed_window_titles=titles,
            allow_high_risk_controls=False,
            allow_geometry_click=False,
            require_approval_for=frozenset({"invoke", "set_value", "select", "click", "focus"}),
            rate_limit_per_minute=60,
        )

    @classmethod
    def development(cls) -> DesktopPolicy:
        """显式的开发/测试宽松预设；生产请用 :meth:`secure`。"""
        return cls()

    @property
    def is_permissive(self) -> bool:
        """没有进程身份约束即视为宽松（仅标题白名单不构成安全边界）。"""
        return not (
            self.allowed_processes
            or self.allowed_process_names
            or self.allowed_process_paths
        )

    def allows_window(
        self,
        window_id: str,
        title: str,
        process_id: str,
        process_name: str = "",
        process_path: str = "",
    ) -> bool:
        # PID 只做精确匹配；禁止前缀匹配，避免 123 放行 1234 / PID 复用被误认。
        if not self.is_permissive:
            pid_ok = process_id in self.allowed_processes
            name_ok = bool(process_name) and (
                process_name.casefold() in self.allowed_process_names
            )
            normalized_path = process_path.replace("/", "\\").casefold()
            path_ok = bool(normalized_path) and (
                normalized_path in self.allowed_process_paths
            )
            if not (pid_ok or name_ok or path_ok):
                return False
        if self.allowed_window_titles and not any(
            hint in title for hint in self.allowed_window_titles
        ):
            return False
        return True

    def is_high_risk_name(self, name: str) -> bool:
        lowered = (name or "").lower()
        return any(keyword.lower() in lowered for keyword in HIGH_RISK_KEYWORDS)

    def approval_for(self, action: str) -> ApprovalPolicy | None:
        if action not in self.require_approval_for:
            return None
        return ApprovalPolicy(
            required=True,
            prompt=f"桌面动作 {action} 需要人工确认",
        )

    def rate_limit(self) -> RateLimitHint | None:
        if self.rate_limit_per_minute is None:
            return None
        return RateLimitHint(count=self.rate_limit_per_minute, window="1m")

    def to_public(self) -> dict[str, Any]:
        return {
            "allowed_processes": sorted(self.allowed_processes),
            "allowed_process_names": sorted(self.allowed_process_names),
            "allowed_process_paths": sorted(self.allowed_process_paths),
            "allowed_window_titles": sorted(self.allowed_window_titles),
            "allow_high_risk_controls": self.allow_high_risk_controls,
            "allow_geometry_click": self.allow_geometry_click,
        }
