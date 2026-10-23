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

import pytest

from yai_core.kernel.compaction import (
    CompactionPolicy,
    render_for_summary,
    select_compaction_range,
)
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


def test_image_cost_is_used_when_trimming_not_just_when_counting() -> None:
    """硬裁剪必须按与总量相同的成本扣减，否则一超预算就会多删整轮。"""
    context = Context("s", max_chars=8500)
    for index in range(3):
        context.add(
            ChatMessage(role="user", content=str(index), parts=[ImagePart(ref="x")])
        )
        context.add(ChatMessage(role="assistant", content="a"))

    users = [m.content for m in context.messages if m.role == "user"]
    assert users == ["1", "2"], "两轮图片仍在预算内（约 8005 字符），不该删到只剩最后一轮"


def test_text_parts_can_trigger_compaction() -> None:
    """片段只计入 total 却不计入压缩阈值，会让长附件永远触发不了摘要。"""
    messages = [ChatMessage(role="system", content="s")]
    for _ in range(3):
        messages.append(
            ChatMessage(role="user", parts=[TextPart(text="x" * 6000)])
        )
        messages.append(ChatMessage(role="assistant", content="a"))

    rng = select_compaction_range(
        messages, CompactionPolicy(hard_max_chars=10000, keep_recent_rounds=1)
    )
    assert rng is not None, "18000 字符的片段必须触发 10000 阈值的摘要选区"


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


# ---------- 内联图片有硬上限 ----------


def test_oversized_inline_image_is_rejected_at_construction() -> None:
    """图片整块进模型请求：超大载荷既烧钱又可能撑爆请求，所以在构造时就拒。"""
    import base64

    from yai_core.types import MAX_INLINE_IMAGE_BYTES

    # 上限按**解码后字节**算，所以载荷要用真实 base64 构造（字符数是字节数的 4/3）。
    huge = base64.b64encode(b"x" * (MAX_INLINE_IMAGE_BYTES + 1)).decode()
    with pytest.raises(ValueError, match="超过上限"):
        ImagePart(media_type="image/png", data=huge)
    # 正常大小不受影响
    assert ImagePart(media_type="image/png", data=PNG).data == PNG


# ---------- 资源引用的生命周期语义被写进数据 ----------


def test_resource_part_carries_scope_and_expiry() -> None:
    part = ResourcePart(
        uri="res://host/42",
        name="report.pdf",
        scope="tenant:a",
        expires_at="2026-12-31T00:00:00+08:00",
    )
    text = part.to_text()
    assert "tenant:a" in text
    assert "2026-12-31" in text
    # 落盘形态保留这两个字段（跨进程不丢语义）
    reference = part.to_reference()
    assert reference["scope"] == "tenant:a"
    assert reference["expires_at"].startswith("2026-12-31")
    restored = parts_from_references([reference])
    assert restored is not None
    assert isinstance(restored[0], ResourcePart)
    assert restored[0].scope == "tenant:a"
