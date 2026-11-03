"""SPI 契约面：十个可替换契约的**数量与形状**必须与 README 承诺一致。

此前「十个可替换 SPI 契约」是 README 首屏可核对的数字，却没有任何测试钉住它：
增删契约、漏改 README 都不会让 CI 变红。本文件把契约清单钉成断言。

同时钉住两件相邻的事：
- ``EventType`` 的每个成员都必须在 ``src/`` 里**有真实发出点**——
  防止出现"定义了却从不发"的死事件（``CODE_TOOL_RETIRED`` 曾长期如此：
  类型在 ``types.py`` 里、示例里有消费分支、白皮书把它列为对外可见事件，
  但内核从头到尾没发过）。
- ``spi/__init__.py`` 的 ``__all__`` 不得漏导出或多导出契约。
"""

from __future__ import annotations

import ast
import pathlib
import re

import yai_core.spi

ROOT = pathlib.Path(__file__).resolve().parents[1]
KERNEL = ROOT / "src" / "yai_core"

#: 十个契约的规范名（顺序即 README 列出的顺序）。
EXPECTED_CONTRACTS = (
    "ModelProvider",
    "Channel",
    "MemoryStore",
    "PermissionPolicy",
    "ToolDiscovery",
    "ToolSandbox",
    "RouteSelector",
    "EmbeddingProvider",
    "StorageCipher",
    "ResourceProvider",
)

#: README「十个可替换 SPI 契约」那一行的契约短名（不含 Provider 等后缀）。
README_CONTRACT_TOKENS = (
    "Model",
    "Channel",
    "Memory",
    "Policy",
    "Discovery",
    "Sandbox",
    "RouteSelector",
    "Embedding",
    "StorageCipher",
    "Resource",
)


def _spi_exports() -> set[str]:
    return set(yai_core.spi.__all__)


def test_spi_exports_exactly_ten_contracts() -> None:
    """``yai_core.spi`` 恰好导出十个契约。"""
    exports = _spi_exports()
    contracts = {name for name in exports if name.endswith("Provider") or name in {
        "Channel",
        "MemoryStore",
        "PermissionPolicy",
        "ToolDiscovery",
        "ToolSandbox",
        "RouteSelector",
        "StorageCipher",
    }}
    missing = [name for name in EXPECTED_CONTRACTS if name not in contracts]
    assert not missing, f"spi 少了契约：{missing}（实际导出：{sorted(contracts)}）"
    assert len(contracts) == len(EXPECTED_CONTRACTS), (
        f"契约数应为 {len(EXPECTED_CONTRACTS)}，实际 {len(contracts)}：{sorted(contracts)}"
    )


def test_every_contract_is_a_protocol() -> None:
    """十个契约都必须是 ``Protocol``（可替换、可鸭子类型），不是具体类。"""
    for name in EXPECTED_CONTRACTS:
        obj = getattr(yai_core.spi, name)
        assert getattr(obj, "_is_protocol", False), f"{name} 不是 Protocol"


def test_readme_lists_the_same_ten_contracts() -> None:
    """README「十个可替换 SPI 契约」那一行必须与实际契约一一对应。"""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(
        r"`Model\s*·\s*Channel\s*·.*?`",
        text,
        flags=re.S,
    )
    assert match is not None, "README 里找不到十个 SPI 契约的清单行"
    line = match.group(0)
    for token in README_CONTRACT_TOKENS:
        assert token in line, f"README 的 SPI 清单里没有 {token}（实际：{line}）"
    assert "十个" in text, "README 不再说「十个 SPI 契约」——契约数变了就要同步改文案"


def test_readme_english_lists_the_same_ten_contracts() -> None:
    """英文门面必须列同一套契约（双语同步）。"""
    text = (ROOT / "README.en.md").read_text(encoding="utf-8")
    for token in README_CONTRACT_TOKENS:
        assert token in text, f"README.en.md 的 SPI 清单里没有 {token}"


def test_spi_modules_exist_one_per_contract() -> None:
    """每个契约有自己的 SPI 模块文件（``spi/<name>.py``），便于按契约检索。"""
    modules = {p.stem for p in (KERNEL / "spi").glob("*.py") if p.stem != "__init__"}
    assert len(modules) == len(EXPECTED_CONTRACTS), (
        f"spi/ 下有 {len(modules)} 个契约模块，与 {len(EXPECTED_CONTRACTS)} 个契约对不上：{modules}"
    )


#: 21 类事件的冻结集合。增删事件必须同时改这里、`EventType`、文档与 walkthrough。
EXPECTED_EVENT_TYPES = frozenset(
    {
        "STRATEGY_SELECTED",
        "PLAN_CREATED",
        "MODEL_MESSAGE",
        "TOOL_CALL",
        "TOOL_RESULT",
        "PERMISSION_ASKED",
        "PERMISSION_DENIED",
        "CLARIFY_REQUESTED",
        "CAPABILITY_MISSING",
        "TOOL_DISCOVERED",
        "TOOL_COMPOSED",
        "CODE_TOOL_CREATED",
        "CODE_TOOL_RETIRED",
        "CONTEXT_COMPACTED",
        "MEMORIES_RECALLED",
        "HOST_STATE",
        "PLAN_REVISED",
        "TIER_CHANGED",
        "BUDGET_EXCEEDED",
        "ERROR",
        "DONE",
    }
)


def _event_type_members() -> list[str]:
    tree = ast.parse((KERNEL / "types.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "EventType":
            return [
                stmt.targets[0].id
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                and isinstance(stmt.targets[0], ast.Name)
            ]
    raise AssertionError("types.py 里找不到 EventType")


def _event_emitters() -> dict[str, list[str]]:
    """扫描真实发出点：``AgentEvent(EventType.X, ...)`` 构造调用所在文件:行号。

    不用文本匹配——注释/docstring/比较表达式里出现成员名不算发出。
    """
    emitted: dict[str, list[str]] = {}
    for path in sorted(KERNEL.rglob("*.py")):
        if path.name == "types.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else (func.attr if isinstance(func, ast.Attribute) else "")
            )
            if name != "AgentEvent" or not node.args:
                continue
            first = node.args[0]
            if (
                isinstance(first, ast.Attribute)
                and isinstance(first.value, ast.Name)
                and first.value.id == "EventType"
            ):
                rel = path.relative_to(KERNEL).as_posix()
                emitted.setdefault(first.attr, []).append(f"{rel}:{node.lineno}")
    return emitted


def test_no_dead_event_types() -> None:
    """每个事件类型都必须有真实的 ``AgentEvent(EventType.X, ...)`` 发出点。"""
    members = _event_type_members()
    assert members, "EventType 没有任何成员"
    emitters = _event_emitters()
    dead = [member for member in members if not emitters.get(member)]
    assert not dead, (
        "以下事件类型定义了却没有 AgentEvent 构造发出点（死事件）："
        f"{dead} —— 要么补上发出点，要么把类型/文档/示例里的它删掉"
    )


def test_event_type_members_are_stable() -> None:
    """事件类型集合冻结（不只是数量）——增删事件必须同步文档与快照。"""
    members = frozenset(_event_type_members())
    assert members == EXPECTED_EVENT_TYPES, (
        f"EventType 集合发生变化：新增 {members - EXPECTED_EVENT_TYPES}，"
        f"删除 {EXPECTED_EVENT_TYPES - members}；请同步文档、walkthrough 与本快照"
    )
