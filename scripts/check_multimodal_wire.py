"""多模态出站形态的**真机**核对脚本（不参与测试套件）。

为什么单独一个脚本：内核测试必须离线（不依赖网络与 API Key），但"我们生成的
内容数组到底被真实后端接受吗"这种问题，只有真机能回答。脚本做两件事：

1. **离线部分（默认）**：打印内核会发出的**确切请求体**，人眼核对格式；
2. **真机部分（给了 Key 才跑）**：真的发一次请求，把后端的回应原样打印。

用法::

    # 1) 只看形态（离线，不需要任何 Key）
    python scripts/check_multimodal_wire.py

    # 2) 真机核对（会消耗一次调用；用你已有的 OpenAI 兼容配置）
    set OPENAI_API_KEY=...            # Windows PowerShell: $env:OPENAI_API_KEY="..."
    set OPENAI_BASE_URL=https://api.deepseek.com
    set LLM_MODEL=deepseek-chat
    python scripts/check_multimodal_wire.py --live

刻意用**最小**的一张 1x1 PNG：真机核对的是"格式被不被接受"，不是"模型看不看得懂"。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from yai_core.types import ChatMessage, ImagePart, ResourcePart, TextPart  # noqa: E402

#: 1x1 透明 PNG（最小合法 PNG）。
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def build_message() -> ChatMessage:
    """构造一条"文本 + 图片 + 资源引用"混合消息（资源引用只内联元数据）。"""
    return ChatMessage(
        role="user",
        content="这张图里有什么？",
        parts=[
            TextPart(text="补充：这是测试消息"),
            ImagePart(media_type="image/png", data=base64.b64encode(PNG_1X1).decode()),
            ResourcePart(
                uri="res://host/42",
                name="report.pdf",
                media_type="application/pdf",
                scope="tenant:a",
                expires_at="2026-12-31T00:00:00+08:00",
            ),
        ],
    )


def show_offline() -> None:
    """离线部分：打印内核会发出的确切消息 dict。"""
    message = build_message()
    payload = message.to_llm_dict()
    print("=== 内核出站形态（OpenAI 兼容） ===")
    print(f"role: {payload['role']}")
    for index, block in enumerate(payload["content"]):
        kind = block.get("type")
        if kind == "text":
            print(f"  [{index}] text: {block['text']!r}")
        elif kind == "image_url":
            url = block["image_url"]["url"]
            print(f"  [{index}] image_url: {url[:48]}...（共 {len(url)} 字符）")
        else:
            print(f"  [{index}] {kind}: {block}")
    print()
    print("=== 降级渲染（不支持多模态的模型 / 摘要用） ===")
    from yai_core.types import parts_to_text

    print(parts_to_text(message.parts))
    print()
    print("=== 落盘形态（只存引用，不存图片字节） ===")
    for part in message.parts:
        print(f"  {part.to_reference()}")


async def run_live() -> int:
    """真机部分：真的发一次请求，打印后端的回应。"""
    try:
        from yai_core.llm.openai_compat import OpenAICompatProvider
    except ImportError as exc:  # pragma: no cover - 需要 openai extra
        print(f"无法导入 OpenAI 兼容后端：{exc}", file=sys.stderr)
        return 2

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("未设置 OPENAI_API_KEY —— 跳过真机部分（--live 需要 Key）", file=sys.stderr)
        return 2

    provider = OpenAICompatProvider()
    message = build_message()
    try:
        response = await provider.achat([message.to_llm_dict()])
    except Exception as exc:  # noqa: BLE001 - 脚本要把后端原话打出来
        print(f"后端拒绝了这次请求：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("=== 后端回应 ===")
    print(response.content[:800] or "（空回应）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="多模态出站形态核对")
    parser.add_argument(
        "--live", action="store_true", help="真的发一次请求（需要 OPENAI_API_KEY）"
    )
    args = parser.parse_args()
    show_offline()
    if args.live:
        return asyncio.run(run_live())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
