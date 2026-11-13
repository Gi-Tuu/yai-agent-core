"""外部文本卫生：把不可信来源的文本收敛成"可安全放进模型上下文的形状"。

2026 年 MCP 生态的公开事实：恶意 MCP Server 可以在工具描述里塞入提示注入/
零宽字符/双向覆盖符（OWASP "MCP Tool Poisoning"、CSA 2026-07 报告都记录了这类
攻击）。工具描述会被拼进模型提示，因此和 ACC 的 guidance 一样属于**不可信输入**：
这里只做字符层净化 + 长度上限，**不做"指令检测"**（语义检测误报高、也挡不住改写）。

设计口径：

- 去掉控制字符（保留 ``\\n``/``\\t``，但它们最终会被折叠成空格）、零宽字符与
  双向覆盖符；这些字符在界面上可以"看不见"，放进提示没有正当用途；
- 折叠连续空白为单个空格，避免用大量换行/制表符做版面欺骗；
- 超长文本截断到 ``max_chars``（含省略号），避免远端用超长描述挤占上下文；
- 纯函数、零依赖，可被 MCP / OpenAPI / 其它外部文本来源复用。
"""

from __future__ import annotations

#: 零宽与双向覆盖符（常见于"看不见的注入"）。
_INVISIBLE = frozenset(
    {
        0x200B,  # zero-width space
        0x200C,  # zero-width non-joiner
        0x200D,  # zero-width joiner
        0x2060,  # word joiner
        0xFEFF,  # zero-width no-break space / BOM
    }
)


def _is_bidi_control(code: int) -> bool:
    return 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069


def sanitize_untrusted_text(
    text: str | None,
    *,
    max_chars: int = 1000,
) -> str:
    """把不可信文本净化成一段安全的单行文本（空输入返回空字符串）。

    Args:
        text: 远端工具描述 / guidance 等不可信文本。
        max_chars: 截断上限（含 ``…``）；必须大于 0。

    Returns:
        去除控制/零宽/双向字符、空白折叠、按长度截断后的文本。
    """
    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")
    if not text:
        return ""
    cleaned: list[str] = []
    for char in str(text):
        code = ord(char)
        if char in ("\n", "\t"):
            cleaned.append(" ")
            continue
        if code < 0x20 or code == 0x7F:
            continue
        if code in _INVISIBLE or _is_bidi_control(code):
            continue
        cleaned.append(char)
    normalized = " ".join("".join(cleaned).split())
    if len(normalized) > max_chars:
        return normalized[: max_chars - 1].rstrip() + "…"
    return normalized
