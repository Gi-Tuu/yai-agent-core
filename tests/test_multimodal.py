"""多模态消息契约：片段能进模型、能在摘要里留痕、落盘不存图片字节。

设计口径（与讲义第 48 篇一致）：

- ``ChatMessage.parts`` **默认 None = 纯文本消息**，旧行为逐字不变；
- 有片段时 ``to_llm_dict()`` 输出 OpenAI 兼容的**内容数组**（该形态是各家的原生形态，
  OpenAI 兼容后端直通即可）；
- 只有**内联**的图片能被模型看到；只有引用的图片一律渲染成"明说模型看不到"的占位，
  **绝不静默丢图**——否则模型会在缺资料的情况下照常回答；
- 落盘**只存引用与元数据，不存图片字节**：历史里不该长期驻留大块二进制。

全程离线，不依赖网络与 API Key。
"""

from __future__ import annotations

import json

from yai_core.kernel.compaction import render_for_summary
from yai_core.kernel.context import Context
from yai_core.llm.anthropic import to_anthropic_messages
from yai_core.memory.sqlite_store import SqliteStore
from yai_core.types import (
    ChatMessage,
    ImagePart,
    ResourcePart,
    TextPart,
    parts_from_references,
    parts_to_text,
)

PNG = "iVBORw0KGgo="


# ---------- 纯文本消息逐字不变 ----------


def test_plain_message_keeps_string_content() -> None:
    message = ChatMessage(role="user", content="你好")
    assert message.to_llm_dict() == {"role": "user", "content": "你好"}
    assert message.parts is None


# ---------- 片段 → 内容数组 ----------


def test_parts_render_as_openai_content_array() -> None:
    message = ChatMessage(
        role="user",
        content="看这张图",
        parts=[ImagePart(media_type="image/png", data=PNG)],
    )
    payload = message.to_llm_dict()
    assert isinstance(payload["content"], list)
    blocks = payload["content"]
    assert blocks[0] == {"type": "text", "text": "看这张图"}
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_text_part_alone_produces_array() -> None:
    message = ChatMessage(role="user", content="", parts=[TextPart(text="片段文本")])
    blocks = message.to_llm_dict()["content"]
    assert blocks == [{"type": "text", "text": "片段文本"}]


# ---------- 引用形态：明说模型看不到，不静默丢 ----------


def test_uninlined_image_becomes_an_explicit_placeholder() -> None:
    part = ImagePart(media_type="image/png", ref="res://host/42")
    text = part.to_text()
    assert "模型看不到" in text
    assert "res://host/42" in text
    block = part.to_openai_dict()
    assert block["type"] == "text" and "看不到" in block["text"]


def test_resource_part_never_claims_to_carry_content() -> None:
    part = ResourcePart(uri="file://secret/report.pdf", name="report.pdf")
    text = part.to_text()
    assert "宿主提供" in text and "report.pdf" in text
    assert part.to_openai_dict()["type"] == "text"


# ---------- 降级渲染：给不支持多模态的模型 / 摘要用 ----------


def test_parts_to_text_keeps_every_part_visible() -> None:
    text = parts_to_text(
        [TextPart(text="文字"), ImagePart(ref="res://x"), ResourcePart(uri="file://y")]
    )
    assert "文字" in text and "看不到" in text and "宿主提供" in text


def test_parts_to_text_on_empty_is_empty() -> None:
    assert parts_to_text(None) == ""
    assert parts_to_text([]) == ""


# ---------- 上下文：片段参与预算与摘要渲染 ----------


def test_context_counts_parts_in_budget() -> None:
    context = Context("系统提示")
    context.add(ChatMessage(role="user", content="看图", parts=[ImagePart(ref="res://x")]))
    assert context.total_chars() > len("系统提示") + len("看图")


def test_compaction_render_mentions_parts() -> None:
    messages = [
        ChatMessage(role="user", content="这是截图", parts=[ImagePart(ref="res://x")]),
    ]
    rendered = render_for_summary(messages, 0, 1)
    assert "这是截图" in rendered
    assert "看不到" in rendered


# ---------- 持久化：只存引用，不存图片字节 ----------


def test_sqlite_persists_references_not_image_bytes(tmp_path) -> None:
    store = SqliteStore(tmp_path / "m.db")
    import asyncio

    asyncio.run(
        store.append_history(
            ChatMessage(
                role="user",
                content="看图",
                parts=[
                    ImagePart(media_type="image/png", data=PNG, ref="res://host/42"),
                    ResourcePart(uri="file://secret/report.pdf", name="report.pdf"),
                ],
            )
        )
    )
    raw = (tmp_path / "m.db").read_bytes()
    assert PNG.encode() not in raw, "图片字节不该落盘"

    restored = store.history()[0]
    assert restored.parts is not None
    kinds = [type(part).__name__ for part in restored.parts]
    assert kinds == ["ImagePart", "ResourcePart"]
    image = restored.parts[0]
    assert isinstance(image, ImagePart)
    assert image.data == "" and image.ref == "res://host/42"
    assert "看不到" in image.to_text()
    store.close()


def test_parts_reference_round_trip() -> None:
    original = [TextPart(text="t"), ImagePart(ref="res://x"), ResourcePart(uri="file://y")]
    payload = [part.to_reference() for part in original]
    restored = parts_from_references(payload)
    assert restored is not None
    assert isinstance(restored[0], TextPart)
    assert isinstance(restored[1], ImagePart)
    assert isinstance(restored[2], ResourcePart)


def test_broken_parts_column_does_not_break_history(tmp_path) -> None:
    store = SqliteStore(tmp_path / "b.db")
    import asyncio

    asyncio.run(store.append_history(ChatMessage(role="user", content="文本还在")))
    store.close()

    import sqlite3

    conn = sqlite3.connect(tmp_path / "b.db")
    conn.execute("UPDATE history SET parts = '{坏 JSON'")
    conn.commit()
    conn.close()

    store2 = SqliteStore(tmp_path / "b.db")
    history = store2.history()
    assert history and history[0].content == "文本还在"
    assert history[0].parts is None
    store2.close()


# ---------- Anthropic 适配器：图片真的被翻译成 Claude 块 ----------


def test_anthropic_translates_inline_image_to_claude_block() -> None:
    messages = to_anthropic_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
                ],
            }
        ]
    )
    blocks = messages[0]["content"]
    assert blocks[0] == {"type": "text", "text": "看这张图"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": PNG,
    }


def test_anthropic_degrades_uninlined_image_to_text() -> None:
    messages = to_anthropic_messages(
        [{"role": "user", "content": [{"type": "text", "text": "看不到"}]}]
    )
    assert messages[0]["content"][0]["type"] == "text"


def test_parts_json_is_stable_for_manifest_style_serialization() -> None:
    """引用形态是纯 JSON 数据：能进清单、能跨进程，不含不可序列化对象。"""
    payload = [part.to_reference() for part in [ImagePart(ref="res://x")]]
    assert json.loads(json.dumps(payload)) == payload
