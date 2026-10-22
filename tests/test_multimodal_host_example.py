"""host_k_multimodal 示例的回归：宿主侧解析器的四条边界。

覆盖：未知句柄 / 越权作用域 / 过期 / 超限 / 路径越界，
以及"模型给的参数无法扩大自己的作用域"。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "host_k_multimodal"))

from resolver import (  # noqa: E402
    HostResource,
    HostResourceResolver,
    ResourceExpiredError,
    ResourceResolutionError,
    ResourceScopeError,
    ResourceTooLargeError,
    UnknownResourceError,
)


def _resolver(tmp_path: Path, **kwargs) -> HostResourceResolver:
    (tmp_path / "notes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "notes" / "todo.md").write_text("- 一件事\n", encoding="utf-8")
    (tmp_path / "notes" / "private.md").write_text("不该被读到\n", encoding="utf-8")
    (tmp_path / "notes" / "big.md").write_text("x" * 5000, encoding="utf-8")
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    resources = {
        "res://note/1": HostResource(
            handle="res://note/1", relative_path="notes/todo.md", scope="tenant:a"
        ),
        "res://note/private": HostResource(
            handle="res://note/private", relative_path="notes/private.md", scope="tenant:b"
        ),
        "res://note/expired": HostResource(
            handle="res://note/expired",
            relative_path="notes/todo.md",
            expires_at=(now - timedelta(hours=1)).isoformat(),
        ),
        "res://note/big": HostResource(
            handle="res://note/big", relative_path="notes/big.md"
        ),
    }
    return HostResourceResolver(
        root=tmp_path, resources=resources, max_bytes=1000, clock=lambda: now, **kwargs
    )


def test_resolves_own_resource(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path)
    text = resolver.resolve("res://note/1", allowed_scopes=("tenant:a",))
    assert "一件事" in text


def test_unknown_handle_is_refused(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path)
    with pytest.raises(UnknownResourceError):
        resolver.resolve("res://note/nope", allowed_scopes=("tenant:a",))


def test_scope_is_decided_by_the_host_not_the_model(tmp_path: Path) -> None:
    """模型能选的只是句柄；作用域判定用宿主自己的清单。"""
    resolver = _resolver(tmp_path)
    with pytest.raises(ResourceScopeError):
        resolver.resolve("res://note/private", allowed_scopes=("tenant:a",))
    # 换一个调用者（宿主给了别的作用域）就能读到——判定确实来自宿主
    assert resolver.resolve("res://note/private", allowed_scopes=("tenant:b",))


def test_expired_reference_is_refused(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path)
    with pytest.raises(ResourceExpiredError):
        resolver.resolve("res://note/expired", allowed_scopes=("tenant:a",))


def test_oversized_resource_is_refused(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path)
    with pytest.raises(ResourceTooLargeError):
        resolver.resolve("res://note/big", allowed_scopes=("tenant:a",))


def test_path_escape_is_refused(tmp_path: Path) -> None:
    """路径越界走内核那套纯函数：闸门认的路径与实际读的路径必须是同一条。"""
    resolver = HostResourceResolver(
        root=tmp_path,
        resources={
            "res://escape": HostResource(
                handle="res://escape", relative_path="../outside.md"
            )
        },
    )
    with pytest.raises(ResourceResolutionError):
        resolver.resolve("res://escape", allowed_scopes=("tenant:a",))
