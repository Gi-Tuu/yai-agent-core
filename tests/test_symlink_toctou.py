import asyncio
import os
import sys
from pathlib import Path

import pytest

from yai_core.batteries.filesystem import FileSystemResources
from yai_core.spi.resources import ResourceError, ResourcePolicy

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
