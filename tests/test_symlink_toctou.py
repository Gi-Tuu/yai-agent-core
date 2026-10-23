import asyncio
import os
import sys
from pathlib import Path

import pytest

from yai_core.batteries.filesystem import FileSystemResources
from yai_core.spi.resources import (
    ResourceError,
    ResourcePolicy,
    _fd_path,
    fd_matches_path,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "host_k_multimodal"))


def _can_symlink(tmp_path: Path) -> bool:
    probe = tmp_path / "probe-link"
    source = tmp_path / "probe-target"
    source.write_text("x", encoding="utf-8")
    try:
        os.symlink(source, probe)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        if probe.exists():
            probe.unlink()


def _provider(tmp_path: Path) -> FileSystemResources:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    (root / "note.txt").write_text("正常内容", encoding="utf-8")
    return FileSystemResources(root, policy=ResourcePolicy())


def test_symlink_inside_root_is_refused_on_every_platform(tmp_path: Path) -> None:
    """指向根目录外的链接在**调用之前**就存在：逐段 lstat 拦住（跨平台一致）。"""
    if not _can_symlink(tmp_path):
        pytest.skip("当前环境无法创建符号链接")
    provider = _provider(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("根目录外的内容", encoding="utf-8")
    (provider.root / "note.txt").unlink()
    os.symlink(outside, provider.root / "note.txt")

    with pytest.raises(ResourceError):
        asyncio.run(provider.read("note.txt"))


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"), reason="当前平台没有 O_NOFOLLOW（Windows）"
)
def test_symlink_swapped_in_after_validation_is_refused(tmp_path: Path) -> None:
    """**校验之后**才被换成的链接：POSIX 上由 O_NOFOLLOW 在内核层拒绝。

    这是"路径校验 → 真正打开"之间的竞态窗口：校验通过之后、打开之前把文件换成
    指向别处的链接，就能把根目录外的内容读出来。``O_NOFOLLOW`` 把窗口关在**内核层**。

    **平台差异**：`O_NOFOLLOW`` 是 POSIX 扩展，**Windows 没有**——那边这个窗口只能
    收窄、不能根除（本测试在 Windows 上被跳过，不是"通过"）。
    """
    if not _can_symlink(tmp_path):
        pytest.skip("当前环境无法创建符号链接")
    provider = _provider(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("根目录外的内容", encoding="utf-8")

    # 先正常读一次（走完校验），再把最后一段换成指向外部的链接
    assert asyncio.run(provider.read("note.txt")).text == "正常内容"
    (provider.root / "note.txt").unlink()
    os.symlink(outside, provider.root / "note.txt")

    with pytest.raises(ResourceError):
        asyncio.run(provider.read("note.txt"))


def test_fd_path_check_detects_a_different_file(tmp_path: Path) -> None:
    """打开后复核不是恒真：句柄指向 A 时，拿 B 去比必须不一致。"""
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    fd = os.open(first, os.O_RDONLY)
    try:
        if _fd_path(fd) is None:
            pytest.skip("当前平台取不到句柄路径")
        assert fd_matches_path(fd, first) is True
        assert fd_matches_path(fd, second) is False
    finally:
        os.close(fd)


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"), reason="当前平台没有 O_NOFOLLOW（Windows）"
)
def test_parent_directory_swap_is_detected_after_open(tmp_path: Path) -> None:
    """父目录被换成链接时 O_NOFOLLOW 保护不到；打开后复核句柄路径把它拒掉。"""
    if not _can_symlink(tmp_path):
        pytest.skip("当前环境无法创建符号链接")
    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (sub / "a.txt").write_text("正常内容", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("根目录外的内容", encoding="utf-8")

    target = sub / "a.txt"
    sub.rename(root / "sub-real")
    os.symlink(outside, sub, target_is_directory=True)

    fd = os.open(target, os.O_RDONLY)  # 沿被换掉的父目录打开
    try:
        assert fd_matches_path(fd, target) is False
    finally:
        os.close(fd)


def test_write_rechecks_target_before_replace(tmp_path: Path, monkeypatch) -> None:
    """临时文件写好、replace 之前父目录被换掉：复核失败，根目录外不落内容。"""
    if not _can_symlink(tmp_path):
        pytest.skip("当前环境无法创建符号链接")
    provider = _provider(tmp_path)
    sub = provider.root / "sub"
    sub.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    import tempfile

    real_mkstemp = tempfile.mkstemp

    def swapping_mkstemp(*args, **kwargs):
        # 校验已完成、临时文件尚未创建时，把目标父目录换成指向根目录外的链接。
        sub.rename(provider.root / "sub-real")
        os.symlink(outside, sub, target_is_directory=True)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", swapping_mkstemp)

    with pytest.raises(ResourceError, match="路径在落盘前发生变化"):
        asyncio.run(provider.create("sub/a.txt", "不该落盘"))
    assert not (outside / "a.txt").exists()
