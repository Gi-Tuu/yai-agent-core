"""host_e 跨进程工具服务：把销售 CRM 的业务能力以本地 HTTP 工具形式开放。

这是"应用自带能力、Core 只负责使用"在跨进程形态下的一面：

- **进程内嵌入**：``AgentCore.auto(crm)`` 直接自省 ``SalesCrm`` 的方法注册工具；
- **跨进程操控**：桌面灵动岛是独立进程，选"销售专员"后通过
  ``POST /api/agent/tool`` 调用本服务暴露的**同名工具**，工具名、参数、返回语义
  与进程内自省完全一致——灵动岛侧不复制任何业务过滤逻辑。

安全边界
--------
- HTTP 服务只监听 ``127.0.0.1``，不面向公网、不做远程鉴权；
- 权限三档（全部 / 部分 / 无需审批）的决策在调用方（灵动岛侧）完成，
  本服务只忠实执行宿主自己的业务方法；
- 只有显式白名单里的方法可被调用，任何下划线私有方法、内部方法都不可达。

本模块是纯业务模块，**不 import yai_core**，与 ``crm_app.py`` 一样可独立运行。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from host_e_sales_crm import catalog_capabilities
from host_e_sales_crm.crm_app import SalesCrm

#: 读工具：只查询、不改变数据（跨进程调用时无需互斥锁）。
READ_TOOLS: tuple[str, ...] = (
    "list_customers",
    "search_customers",
    "get_customer",
    "list_orders",
    "sum_amount",
    "list_opportunities",
    "list_followups",
    "customers_due_followup",
    "list_todos",
    "daily_brief",
)

#: 写工具：会改变 CRM 数据（跨进程调用时与网页原生写操作走同一把互斥锁）。
WRITE_TOOLS: tuple[str, ...] = (
    "add_customer",
    "update_customer",
    "add_followup",
    "create_order",
    "create_opportunity",
    "update_opportunity_stage",
    "create_todo",
    "complete_todo",
    "delete_todo",
    "delete_order",
    "delete_opportunity",
    "delete_customer",
)

#: 按需能力：默认不注册，命中"能力缺口"才被调用方发现（与进程内 catalog 对应）。
DISCOVERY_TOOLS: tuple[str, ...] = (
    "get_visit_weather",
    "calc_quote_with_tax",
)

#: 这些参数从 JSON 进来时统一成整数 / 浮点（JSON 文本或字符串数字都容忍）。
_INT_PARAMS = frozenset({"days"})
_FLOAT_PARAMS = frozenset({"rate"})


class UnknownToolError(KeyError):
    """请求的工具名不在白名单内。"""


class ToolArgumentError(TypeError):
    """工具参数缺失或类型无法转换。"""


class ToolService:
    """按显式白名单把 CRM / 按需能力包装成可跨进程调用的工具。

    工具名 → 绑定方法的映射在构造时一次性确定；参数名由方法签名反射得到，
    调用方只能按签名传参，多余参数被忽略，缺失参数走方法默认值。
    """

    def __init__(self, crm: SalesCrm) -> None:
        self.crm = crm
        targets: dict[str, Callable[..., Any]] = {}
        for name in READ_TOOLS:
            targets[name] = getattr(crm, name)
        for name in WRITE_TOOLS:
            targets[name] = getattr(crm, name)
        for name in DISCOVERY_TOOLS:
            targets[name] = getattr(catalog_capabilities, name)
        self._targets = targets
        self._signatures = {name: inspect.signature(fn) for name, fn in targets.items()}

    @property
    def write_tools(self) -> frozenset[str]:
        """写工具名集合，供 HTTP 层决定是否加互斥锁。"""
        return frozenset(WRITE_TOOLS)

    def is_write(self, name: str) -> bool:
        return name in self.write_tools

    def describe(self) -> list[dict[str, str]]:
        """返回工具名 + 一句话说明（供调试端点；跨进程自省以客户端 docstring 为准）。"""
        rows: list[dict[str, str]] = []
        for group, names in (
            ("read", READ_TOOLS),
            ("write", WRITE_TOOLS),
            ("discovery", DISCOVERY_TOOLS),
        ):
            for name in names:
                doc = (inspect.getdoc(self._targets[name]) or "").strip().splitlines()
                summary = doc[0] if doc else ""
                rows.append({"group": group, "name": name, "summary": summary})
        return rows

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """按白名单名调用一个工具，参数按方法签名绑定并做基础类型转换。"""
        if name not in self._targets:
            raise UnknownToolError(name)
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolArgumentError("arguments 必须是对象（参数名 → 值）")

        signature = self._signatures[name]
        kwargs: dict[str, Any] = {}
        for param_name in signature.parameters:
            if param_name not in arguments:
                continue  # 缺省参数走方法默认值
            kwargs[param_name] = self._coerce(param_name, arguments[param_name])
        return self._targets[name](**kwargs)

    @staticmethod
    def _coerce(param_name: str, value: Any) -> Any:
        """把 JSON 传入的值按参数名收敛到业务方法期望的类型。"""
        if param_name in _INT_PARAMS:
            try:
                return int(float(value))
            except (TypeError, ValueError) as exc:
                raise ToolArgumentError(f"参数 {param_name!r} 必须是整数") from exc
        if param_name in _FLOAT_PARAMS:
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise ToolArgumentError(f"参数 {param_name!r} 必须是数字") from exc
        return value
