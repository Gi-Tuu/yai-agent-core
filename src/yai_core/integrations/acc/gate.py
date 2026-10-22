"""把 ACC 声明编译成**工具自带的准入判定**（kernel 不认识 ACC，只吃一个 callable）。

设计口径：内核执行器不 import 本模块，也不解释 ACC 字段——集成层在校验完声明后，
用 :func:`build_gate` 产出一个闭包挂到 ``ToolSpec.governance.gate`` 上。这样：

- 内核保持协议无关（宿主自带的策略引擎可以挂同一个口子，不必懂 ACC）；
- 判定发生在**每次调用时**，用当时的参数与当时的主体，而不是注册时的一次性快照；
- 声明原件仍留在 ``governance.acc`` 里做透明记录，但**它不产生任何执行效果**。

闭包只返回三种话：拒绝（这次调用不该到达业务操作）、要人工确认（把闸门往上抬）、
或者沉默。抬闸门的方向只有一个——宿主的权限策略永远可以拒绝得更狠，
外部声明永远不能替宿主放行。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from yai_core.integrations.acc.declaration import AccDeclaration
from yai_core.integrations.acc.evaluate import (
    DECISION_APPROVAL_REQUIRED,
    DECISION_REJECT_INVALID_ARGUMENTS,
    approval_outcome,
    effective_risk,
)
from yai_core.types import GateDecision


def build_gate(
    declaration: AccDeclaration, input_schema: dict[str, Any]
) -> Callable[[dict[str, Any], Any], GateDecision]:
    """由已校验的声明 + 绑定输入 schema 造一个准入判定闭包。"""

    def gate(arguments: dict[str, Any], subject: Any = None) -> GateDecision:
        scope = declaration.scope
        if not declaration.enabled:
            # 声明说"这个能力不开放"，闸就必须拦——不能只靠暴露层过滤：
            # 宿主手工用 enabled=false 的声明造闸时，这里是最后一道兜底。
            return GateDecision(
                reject=f"该能力声明为未开放（scope {scope}），已拒绝执行",
                scope=scope,
            )
        if declaration.subject_required and subject is None:
            return GateDecision(
                reject=(
                    f"该能力要求可信操作主体（scope {scope}），"
                    "当前运行上下文没有宿主注入的主体，拒绝执行"
                ),
                scope=scope,
            )
        outcome = approval_outcome(declaration, arguments, input_schema)
        risk = effective_risk(declaration)
        if outcome.decision == DECISION_REJECT_INVALID_ARGUMENTS:
            return GateDecision(
                reject=f"调用被能力闸拒绝：{outcome.reason}",
                reason=outcome.reason,
                risk=risk,
                scope=scope,
            )
        if outcome.decision == DECISION_APPROVAL_REQUIRED:
            return GateDecision(
                require_approval=True,
                reason=outcome.reason,
                prompt=declaration.approval_prompt,
                risk=risk,
                scope=scope,
            )
        return GateDecision(risk=risk, scope=scope)

    return gate
