"""父子核 Phase 1 演示：ATM（父核）调用文档（子核）写一条流水。零 Key、离线。

    python examples/host_m_federation/run.py

真实生态里两个核在两个产品进程里（Phase 2 走本机 IPC）；本示例把两个核放在同一进程，
只演示"父核注册子核工具 → 模型调用 → 子核执行 → 结果回流"这条链路与治理字段。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if (_ROOT / "src" / "yai_core").is_dir():
    sys.path.insert(0, str(_ROOT / "src"))
if (_ROOT / "shell").is_dir():
    sys.path.insert(0, str(_ROOT))

from shell.federation import ChildCore, Federation  # noqa: E402

from yai_core import (  # noqa: E402
    AgentCore,
    EventType,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    build_spec,
)

#: 文档产品里的"文档内容"（子核的数据，父核摸不到）。
DOCUMENT: list[tuple[str, str]] = []


def add_entry(title: str, body: str = "") -> str:
    """向文档追加一条记录。"""
    DOCUMENT.append((title, body))
    return f"已写入「{title}」"


def list_entries() -> list[str]:
    """列出文档里的记录标题。"""
    return [title for title, _ in DOCUMENT]


def main() -> None:
    # 子核：文档产品里的独立 AgentCore（本示例不跑模型，只暴露工具）
    doc_core = AgentCore(ScriptedModel([ModelResponse(content="idle")]))
    doc_core.register_tools([build_spec(add_entry), build_spec(list_entries)])

    # 父核：ATM 产品里的 AgentCore，模型选择调用子核工具
    atm_model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="doc__add_entry",
                        arguments={"title": "ATM 流水 0001", "body": "转出 100 元"},
                    )
                ],
            ),
            ModelResponse(content="已把今天的转账流水记进文档。"),
        ]
    )
    atm_core = AgentCore(atm_model)
    federation = Federation(atm_core, node_id="atm")
    grant = federation.register_child(
        ChildCore(doc_core, node_id="doc", child_id="doc-app@0.28", product="doc"),
        tools=["add_entry"],
    )

    print("父核可用工具：", [tool["name"] for tool in atm_core.list_tools()])
    print("grant：", {"edge": grant.edge, "tools": sorted(grant.tools)})
    print("-" * 50)

    async def drive() -> None:
        async for event in atm_core.astream("把今天的转账流水记到文档"):
            if event.type == EventType.STRATEGY_SELECTED:
                print(f"[路由] {event.data['strategy']}")
            elif event.type == EventType.TOOL_CALL:
                print(f"[父核→子核] {event.data['tool']}({event.data['arguments']})")
            elif event.type == EventType.DONE:
                print(f"[完成] {event.data['final_text']}")

    asyncio.run(drive())
    print("文档内容：", DOCUMENT)


if __name__ == "__main__":
    main()
