"""点号路径解析：**调用值**与**绑定 schema** 的唯一定义处。

为什么单独成模块：审批条件、脱敏路径、清单导入校验三处都要"按同一条路径找到同一个
东西"。解析器各写一遍时，最危险的不是解析失败，而是**一半命中一半不命中**——
条件静默失效，宿主以为有护栏、实际没有（真实事故：schema 侧会下钻数组、取值侧不会，
于是 ``rows.amount == 5000`` 永远不命中）。

统一后的路径语法（dot-notation）：

- ``headers.authorization``：对象成员；
- ``rows.0.amount``：显式下标进数组；
- ``rows.amount``：**自动下钻**数组——取每个元素上同名字段的所有值（与 schema 侧
  ``items`` 下钻对齐）。取值侧是"候选集合"，判定语义是 ANY（命中任一候选即命中），
  方向偏保守（宁可多问一次人，不可漏问）。

三条不变量：

1. schema 侧能解析的路径，取值侧也一定能解析到"存在与否"的确定答案（不存在就是空集）；
2. 路径解析不到**不报错、不拒绝**——路径不存在 ≠ 条件命中，判断交给调用方；
3. 判定"命中"与"无法判定"是两回事：后者必须失败关闭（见 :mod:`yai_core.policy.approval`）。

本模块零第三方依赖、零副作用，纯函数。
"""

from __future__ import annotations

from typing import Any

__all__ = ["schema_at_path", "value_candidates"]


def value_candidates(args: Any, path: str) -> list[Any]:
    """按点号路径取调用值的**全部候选**；路径不存在返回空列表。

    数组语义与 :func:`schema_at_path` 对齐：

    - ``rows.0.amount``：显式下标；
    - ``rows.amount``：自动下钻，取每个元素上同名字段的值（元素缺该字段则跳过）。
    """
    if not path:
        return []
    current: list[Any] = [args]
    for part in path.split("."):
        if not part:
            return []
        nxt: list[Any] = []
        for node in current:
            if isinstance(node, dict):
                if part in node:
                    nxt.append(node[part])
            elif isinstance(node, list):
                if part.isdigit():
                    index = int(part)
                    if index < len(node):
                        nxt.append(node[index])
                else:
                    for item in node:
                        if isinstance(item, dict) and part in item:
                            nxt.append(item[part])
        current = nxt
        if not current:
            return []
    return current


def schema_at_path(schema: Any, path: str) -> dict[str, Any] | None:
    """按点号路径在输入 schema 里找对应子 schema；找不到返回 ``None``。

    数组处理与 :func:`value_candidates` 对齐：``rows.amount`` 下钻到 ``items``，
    ``rows.0.amount`` 里的下标段只用于选元素（形状与 ``items`` 相同）。
    """
    if not path:
        return None
    current: Any = schema
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        if current.get("type") == "array" and isinstance(current.get("items"), dict):
            current = current["items"]
            if part.isdigit():
                # 下标段只负责选元素，schema 形状与 items 一致。
                continue
        properties = current.get("properties")
        if not isinstance(properties, dict):
            return None
        current = properties.get(part)
    return current if isinstance(current, dict) else None
