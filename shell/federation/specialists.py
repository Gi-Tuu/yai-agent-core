"""把"应用对象"接成联邦子核：桌面灵动岛多专员的桥。

灵动岛里的每个专员本质上是"一个被 AI 化的应用"——对象上的公开方法就是宿主能力。
本模块把这些方法内省成工具、包成 :class:`ChildCore`，再注册进父核联邦：

    app = DemoNotes()
    attach_specialists(federation, {"notes": app, "warehouse": DemoWarehouse()})

之后父核模型就能调用 ``notes__add_note`` / ``warehouse__get_stock``；
子应用仍可独立运行（它自己的工具、策略、状态都在自己的核里）。
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping

from shell.federation.grants import CoreGrant
from shell.federation.node import ChildCore
from shell.federation.parent import Federation
from yai_core import AgentCore, ModelResponse, ScriptedModel, build_spec


def build_specialist_child(
    app: object,
    *,
    node_id: str,
    child_id: str = "",
    product: str = "",
) -> ChildCore:
    """把应用对象的公开方法内省成子核工具。

    Raises:
        ValueError: 对象没有任何可内省的公开方法（只会是错误装配，不静默给空核）。
        TypeError: 某个公开方法无法生成工具 schema（签名/注解不合法）。
    """
    specs = []
    for name, member in inspect.getmembers(app, predicate=inspect.ismethod):
        if name.startswith("_"):
            continue
        try:
            specs.append(build_spec(member))
        except (TypeError, ValueError) as exc:
            raise TypeError(f"专业专员 {node_id!r} 的方法 {name!r} 无法内省：{exc}") from exc
    if not specs:
        raise ValueError(f"应用 {node_id!r} 没有可内省的公开方法")
    core = AgentCore(ScriptedModel([ModelResponse(content="idle")]))
    core.register_tools(specs)
    return ChildCore(
        core,
        node_id=node_id,
        child_id=child_id or node_id,
        product=product or node_id,
    )


def attach_specialists(
    federation: Federation,
    apps: Mapping[str, object],
    *,
    prefix_of: Mapping[str, str] | None = None,
) -> dict[str, CoreGrant]:
    """把一组"专员应用对象"注册成父核的子核，返回每条的 grant。"""
    grants: dict[str, CoreGrant] = {}
    for name, app in apps.items():
        child = build_specialist_child(app, node_id=name)
        prefix = (prefix_of or {}).get(name, name)
        grants[name] = federation.register_child(child, prefix=prefix)
    return grants

