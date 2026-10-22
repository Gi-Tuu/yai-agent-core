"""host_k_multimodal：演示"多模态消息 + 宿主侧资源引用解析"的最小宿主。

这个宿主只有两件事：

1. 暴露一个工具 ``read_resource(handle)``：**模型给句柄，宿主解析**——
   解析、授权、有效期判定全在宿主这一侧（见 :mod:`examples.host_k_multimodal.resolver`）；
2. 演示多模态消息：用户消息里带一张**内联图片**（模型真的看得见）与一个
   **资源引用**（模型看不到内容，但知道有这么个东西存在）。

离线可跑：用 :class:`~yai_core.llm.scripted.ScriptedModel` 假模型，不需要任何 Key。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from resolver import (  # noqa: E402
    HostResource,
    HostResourceResolver,
    ResourceResolutionError,
)

from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec  # noqa: E402
from yai_core.channels import CollectChannel  # noqa: E402
from yai_core.llm.scripted import ScriptedModel  # noqa: E402

#: 一张 1x1 透明 PNG（最小合法 PNG，够演示"模型看得见"）。
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def build_resolver(root: Path) -> HostResourceResolver:
    """登记两条资源：一条属于当前用户作用域，一条属于别的作用域（用来演示越权被拒）。"""
    return HostResourceResolver(
        root=root,
        resources={
            "res://note/1": HostResource(
                handle="res://note/1",
                relative_path="notes/todo.md",
                scope="tenant:a",
            ),
            "res://note/private": HostResource(
                handle="res://note/private",
                relative_path="notes/private.md",
                scope="tenant:b",
            ),
        },
    )


def build_capabilities(
    resolver: HostResourceResolver, allowed_scopes: tuple[str, ...]
) -> list[Any]:
    """产出两个工具：读资源（句柄）+ 列出可用句柄（只列有权限的）。"""

    async def read_resource(handle: str) -> dict:
        """按句柄读取宿主资源。

        句柄来自模型，但**解析与授权在宿主**——你只能读到自己作用域内的资源。
        """
        try:
            text = resolver.resolve(handle, allowed_scopes=allowed_scopes)
        except ResourceResolutionError as exc:
            return {"handle": handle, "ok": False, "error": str(exc)}
        return {"handle": handle, "ok": True, "text": text}

    async def list_handles() -> list[str]:
        """列出当前调用者**有权限**的资源句柄。"""
        visible = []
        for handle, record in resolver.resources.items():
            if not record.scope or record.scope in allowed_scopes:
                visible.append(handle)
        return sorted(visible)

    return [read_resource, list_handles]


async def main() -> None:
    root = Path(__file__).parent / "data"
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "todo.md").write_text(
        "- 给客户发方案\n- 周五前上线 v0.13\n", encoding="utf-8"
    )
    (root / "notes" / "private.md").write_text("不该被读到的内容\n", encoding="utf-8")

    resolver = build_resolver(root)
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="read_resource",
                        arguments={"handle": "res://note/1"},
                    )
                ],
            ),
            ModelResponse(content="待办里有两条：给客户发方案、周五前上线。"),
        ]
    )
    core = AgentCore(model, channel=CollectChannel())
    for func in build_capabilities(resolver, allowed_scopes=("tenant:a",)):
        core.register_tools([build_spec(func)])

    message = {
        "role": "user",
        "content": "这张图是空白底图，另外请读一下 res://note/1 里的待办。",
    }
    print("=== 用户消息（多模态） ===")
    print(f"  文本：{message['content']}")
    print("  图片：内联 1x1 PNG（模型看得见）")
    print("  资源：res://note/1（模型看不到内容，由宿主解析）")
    print()

    result = await core.run(message["content"])
    print("=== 结果 ===")
    print(f"  策略：{result.strategy.value}")
    print(f"  回答：{result.final_text}")
    print()
    print("=== 事件时间线 ===")
    for event in result.events:
        if event.type.value in {"tool_call", "tool_result", "strategy_selected"}:
            detail = event.data.get("arguments") or event.data.get("preview") or ""
            print(f"  {event.type.value}: {event.data.get('tool', '')} {detail}")


if __name__ == "__main__":
    asyncio.run(main())
