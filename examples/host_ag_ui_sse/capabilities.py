"""AG-UI SSE 示例的本地能力：让演示中出现一次真实的 tool call。"""

from __future__ import annotations


def demo_fact(topic: str) -> str:
    """Return a deterministic demo fact for the given topic (offline tool).

    Args:
        topic: The topic to look up; any text is accepted.

    Returns:
        A short deterministic fact string.
    """
    return f"AG-UI demo fact about {topic}: the event stream is the only exit."
