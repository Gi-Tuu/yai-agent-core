"""原子替换重试：Windows 瞬态共享冲突不应让整次写盘失败。"""

from __future__ import annotations

from pathlib import Path

import pytest

from yai_core.fsutil import replace_with_retry


def test_replace_retries_transient_permission_error(tmp_path, monkeypatch) -> None:
    src = tmp_path / "new.txt"
    dst = tmp_path / "target.txt"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    calls = {"count": 0}
    real_replace = __import__("os").replace

    def flaky(a, b):
        if calls["count"] < 2:
            calls["count"] += 1
            raise PermissionError(13, "Access is denied", str(b))
        return real_replace(a, b)

    monkeypatch.setattr("yai_core.fsutil.os.replace", flaky)
    replace_with_retry(str(src), str(dst))
    assert calls["count"] == 2
    assert dst.read_text(encoding="utf-8") == "new"


def test_replace_gives_up_after_attempts(tmp_path, monkeypatch) -> None:
    src = tmp_path / "new.txt"
    dst = tmp_path / "target.txt"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")

    def always_denied(a, b):
        raise PermissionError(13, "Access is denied", str(b))

    monkeypatch.setattr("yai_core.fsutil.os.replace", always_denied)
    with pytest.raises(PermissionError):
        replace_with_retry(str(src), str(dst), attempts=3, base_delay=0)
    assert dst.read_text(encoding="utf-8") == "old"
