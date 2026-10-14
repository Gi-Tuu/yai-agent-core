"""文件资源后端的安全回归：路径越权、并发冲突、写失败恢复、限额。

任务书要求的必测矩阵都在这里，且**双平台可跑**：符号链接相关用例在
没有建链权限的环境（Windows 默认、部分 CI）自动跳过，其余用例两边都必须过。
"""

from __future__ import annotations

import asyncio
import os
import stat as stat_module
from pathlib import Path

import pytest

from yai_core.batteries.filesystem import FileSystemResources, filesystem_capabilities
from yai_core.spi.resources import (
    PathEscapeError,
    ResourceConflictError,
    ResourceError,
    ResourceNotFoundError,
    ResourcePolicy,
    ResourceProvider,
    ResourceTooLargeError,
    normalize_relative,
    resolve_within_root,
    sha256_text,
)


def _can_symlink(target: Path, *, directory: bool = False) -> bool:
    probe = target / "probe-link"
    source = target / ("src-dir" if directory else "src-file.txt")
    source.mkdir() if directory else source.write_text("x", encoding="utf-8")
    try:
        os.symlink(source, probe, target_is_directory=directory)
        probe.unlink()
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        if probe.exists():
            probe.unlink()


@pytest.fixture()
def root(tmp_path: Path) -> FileSystemResources:
    base = tmp_path / "workspace"
    base.mkdir()
    return FileSystemResources(base)


def _run(coro):
    return asyncio.run(coro)


# ---------- 路径形状：非法形状一律拒，不做"清洗后放行" ----------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "..",
        "../outside.txt",
        "notes/../../outside.txt",
        ".",
        "./",
        "/etc/passwd",
        "\\\\server\\share\\file.txt",
        "C:\\Windows\\win.ini",
        "notes/../secrets.txt",
        "~/.ssh/id_rsa",
        "a" * 600,
        "one/two/three/four/five/six/seven/eight/nine/ten/eleven/twelve/thirteen/fourteen/fifteen/sixteen/seventeen",
        "notes\x00.txt",
        "notes\nx.txt",
        "CON",
        "con.txt",
        "NUL/data",
        "notes/bad.",
        "a\\b/c",
        "file:stream",
    ],
)
def test_rejected_path_shapes(bad: str) -> None:
    with pytest.raises(PathEscapeError):
        normalize_relative(bad)


@pytest.mark.parametrize("bad", [None, 12, ["notes.txt"], {"p": "a"}])
def test_non_string_paths_rejected(bad: object) -> None:
    with pytest.raises(PathEscapeError):
        normalize_relative(bad)


@pytest.mark.parametrize(
    "good,expected",
    [
        ("notes/todo.md", "notes/todo.md"),
        ("./notes/todo.md", "notes/todo.md"),
        ("notes//todo.md", "notes/todo.md"),
        ("notes\\todo.md", "notes/todo.md"),
        ("中文笔记 第一版.md", "中文笔记 第一版.md"),
        ("a.b.c.txt", "a.b.c.txt"),
        ("notes/", "notes"),
        ("notes/./", "notes"),
        ("reader's note.txt", "reader's note.txt"),
    ],
)
def test_accepted_path_shapes(good: str, expected: str) -> None:
    assert normalize_relative(good) == expected


# ---------- 归属判定：穿越与符号链接 ----------


def test_resolve_stays_inside_root(root: FileSystemResources) -> None:
    target = resolve_within_root(root.root, "notes/todo.md")
    assert target.is_relative_to(root.root)
    assert target.name == "todo.md"


def test_symlink_escape_rejected(root: FileSystemResources, tmp_path: Path) -> None:
    if not _can_symlink(root.root):
        pytest.skip("当前环境无符号链接权限")
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("别人的文件", encoding="utf-8")
    (root.root / "link.txt").symlink_to(secret)
    with pytest.raises(PathEscapeError):
        _run(root.read("link.txt"))
    with pytest.raises(PathEscapeError):
        resolve_within_root(root.root, "link.txt")


def test_symlinked_directory_component_rejected(root: FileSystemResources, tmp_path: Path) -> None:
    if not _can_symlink(root.root, directory=True):
        pytest.skip("当前环境无目录符号链接权限")
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("越界内容", encoding="utf-8")
    (root.root / "sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathEscapeError):
        resolve_within_root(root.root, "sub/secret.txt")


def test_allow_symlinks_policy_still_blocks_escape(
    root: FileSystemResources, tmp_path: Path
) -> None:
    """放开"逐段拦链接"之后，归属复核这道闸必须仍然拦住指向 root 外的链接。"""
    if not _can_symlink(root.root):
        pytest.skip("当前环境无符号链接权限")
    secret = tmp_path / "outside2.txt"
    secret.write_text("still outside", encoding="utf-8")
    (root.root / "link2.txt").symlink_to(secret)
    permissive = FileSystemResources(
        root.root, policy=ResourcePolicy(allow_symlinks=True)
    )
    with pytest.raises(PathEscapeError):
        resolve_within_root(permissive.root, "link2.txt", policy=permissive.policy)


def test_cross_tenant_read_denied(tmp_path: Path) -> None:
    tenant_a = FileSystemResources(_make_root(tmp_path, "a"))
    tenant_b = FileSystemResources(_make_root(tmp_path, "b"))
    _run(tenant_b.create("notes.txt", "B 租户的秘密"))
    with pytest.raises(PathEscapeError):
        _run(tenant_a.read("../b/notes.txt"))
    with pytest.raises(PathEscapeError):
        _run(tenant_a.create("../b/injected.txt", "x"))


def _make_root(tmp_path: Path, name: str) -> Path:
    base = tmp_path / name
    base.mkdir()
    return base


# ---------- 读写与并发 ----------


def test_provider_satisfies_contract(root: FileSystemResources) -> None:
    assert isinstance(root, ResourceProvider)


def test_create_then_read_roundtrip(root: FileSystemResources) -> None:
    created = _run(root.create("notes/todo.md", "买轴承"))
    assert created.sha256 == sha256_text("买轴承")
    read = _run(root.read("notes/todo.md"))
    assert read.text == "买轴承"
    assert read.path == "notes/todo.md"  # 回给模型的是相对路径，不是绝对路径
    assert not str(read.path).startswith("/")


def test_nested_parents_created(root: FileSystemResources) -> None:
    _run(root.create("a/b/c/deep.txt", "深"))
    assert (root.root / "a/b/c/deep.txt").is_file()


def test_create_never_overwrites(root: FileSystemResources) -> None:
    _run(root.create("keep.txt", "第一版"))
    with pytest.raises(ResourceConflictError):
        _run(root.create("keep.txt", "第二版"))
    assert _run(root.read("keep.txt")).text == "第一版"
    assert _leftover_tmp(root) == []


def test_patch_requires_matching_fingerprint(root: FileSystemResources) -> None:
    first = _run(root.create("plan.md", "v1"))
    _run(root.patch("plan.md", "v2", expected_sha256=first.sha256))
    # 拿着 v1 的指纹再改一次 = 典型的"别人已经改过"：必须冲突，不能硬写
    with pytest.raises(ResourceConflictError):
        _run(root.patch("plan.md", "v3", expected_sha256=first.sha256))
    assert _run(root.read("plan.md")).text == "v2"


def test_patch_with_fresh_fingerprint_succeeds(root: FileSystemResources) -> None:
    _run(root.create("plan.md", "v1"))
    first = _run(root.patch("plan.md", "v2", expected_sha256=_run(root.read("plan.md")).sha256))
    second = _run(root.patch("plan.md", "v3", expected_sha256=first.sha256))
    assert second.sha256 == sha256_text("v3")


@pytest.mark.parametrize("bad", ["", "   ", None, 12, ["abc"]])
def test_patch_rejects_missing_fingerprint(root: FileSystemResources, bad: object) -> None:
    _run(root.create("plan.md", "v1"))
    with pytest.raises(ResourceError):
        _run(root.patch("plan.md", "v2", expected_sha256=bad))  # type: ignore[arg-type]


def test_patch_missing_file_is_not_found(root: FileSystemResources) -> None:
    with pytest.raises(ResourceNotFoundError):
        _run(root.patch("nope.md", "x", expected_sha256="0" * 64))


def test_concurrent_modification_detected(root: FileSystemResources) -> None:
    """模拟"读到 → 别人改了 → 我再写"：指纹必须失效，写被拒。"""
    _run(root.create("shared.md", "A 写的"))
    seen_by_me = _run(root.read("shared.md")).sha256
    (root.root / "shared.md").write_text("B 改过了", encoding="utf-8")
    with pytest.raises(ResourceConflictError):
        _run(root.patch("shared.md", "我的改动", expected_sha256=seen_by_me))
    assert (root.root / "shared.md").read_text(encoding="utf-8") == "B 改过了"


def test_toctou_symlink_swap_between_read_and_patch_rejected(
    root: FileSystemResources, tmp_path: Path
) -> None:
    """读的时候是普通文件，写之前被换成符号链接：每次调用都重新判定归属，必须拒。"""
    if not _can_symlink(root.root):
        pytest.skip("当前环境无符号链接权限")
    _run(root.create("note.txt", "原文"))
    outside = tmp_path / "victim.txt"
    outside.write_text("外部内容", encoding="utf-8")
    (root.root / "note.txt").unlink()
    (root.root / "note.txt").symlink_to(outside)
    with pytest.raises(PathEscapeError):
        _run(root.patch("note.txt", "改写", expected_sha256=sha256_text("原文")))
    assert outside.read_text(encoding="utf-8") == "外部内容"


# ---------- 限额与只读 ----------


def test_read_size_limit_enforced(tmp_path: Path) -> None:
    base = tmp_path / "limited"
    base.mkdir()
    tight = FileSystemResources(base, policy=ResourcePolicy(max_read_bytes=10))
    (base / "big.txt").write_text("远超十个字节的内容", encoding="utf-8")
    with pytest.raises(ResourceTooLargeError):
        _run(tight.read("big.txt"))


def test_write_size_limit_enforced(tmp_path: Path) -> None:
    base = tmp_path / "limited2"
    base.mkdir()
    tight = FileSystemResources(base, policy=ResourcePolicy(max_write_bytes=8))
    with pytest.raises(ResourceTooLargeError):
        _run(tight.create("big.txt", "十二个字符的内容"))
    assert not (base / "big.txt").exists()
    assert _leftover_tmp_files(base) == []


def test_list_truncated_flag(tmp_path: Path) -> None:
    base = tmp_path / "many"
    base.mkdir()
    for index in range(5):
        (base / f"f{index}.txt").write_text("x", encoding="utf-8")
    provider = FileSystemResources(base, policy=ResourcePolicy(max_list_entries=3))
    entries = _run(provider.list(""))
    assert len(entries) == 3
    assert _run(provider.read("f0.txt")).text == "x"


def test_list_marks_directories_and_rejects_file_target(root: FileSystemResources) -> None:
    _run(root.create("notes/a.txt", "1"))
    entries = _run(root.list("notes"))
    assert [entry.name for entry in entries] == ["a.txt"]
    assert entries[0].is_dir is False
    with pytest.raises(ResourceError):
        _run(root.list("notes/a.txt"))
    with pytest.raises(ResourceNotFoundError):
        _run(root.list("nope"))


def test_read_directory_and_missing_and_binary(root: FileSystemResources) -> None:
    with pytest.raises(ResourceNotFoundError):
        _run(root.read("ghost.txt"))
    (root.root / "dir").mkdir()
    with pytest.raises(ResourceError):
        _run(root.read("dir"))
    (root.root / "bin.dat").write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(ResourceError, match="UTF-8"):
        _run(root.read("bin.dat"))


def test_read_only_mount_blocks_writes(root: FileSystemResources) -> None:
    ro = FileSystemResources(root.root, read_only=True)
    with pytest.raises(ResourceError, match="只读"):
        _run(ro.create("x.txt", "x"))
    with pytest.raises(ResourceError, match="只读"):
        _run(ro.patch("x.txt", "y", expected_sha256="0" * 64))
    _run(ro.list(""))  # 读与列举照常


# ---------- 写失败恢复 ----------


def _leftover_tmp(provider: FileSystemResources) -> list[Path]:
    return _leftover_tmp_files(provider.root)


def _leftover_tmp_files(base: Path) -> list[Path]:
    return sorted(p.name for p in base.rglob(".*.tmp"))


def test_failed_fsync_leaves_original_intact_and_no_tmp(
    root: FileSystemResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(root.create("doc.md", "原版内容"))
    calls = {"n": 0}
    real_fsync = os.fsync

    def boom(fd: int) -> None:
        calls["n"] += 1
        raise OSError("模拟写盘失败")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        _run(root.patch("doc.md", "改写内容", expected_sha256=_run(root.read("doc.md")).sha256))
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert calls["n"] == 1
    assert _run(root.read("doc.md")).text == "原版内容"
    assert _leftover_tmp(root) == []


def test_failed_replace_leaves_original_intact(
    root: FileSystemResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(root.create("doc.md", "原版"))
    sha = _run(root.read("doc.md")).sha256

    def boom(src: str, dst: str) -> None:
        raise OSError("模拟 rename 失败")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        _run(root.patch("doc.md", "新版", expected_sha256=sha))
    assert _run(root.read("doc.md")).text == "原版"
    assert _leftover_tmp(root) == []


def test_hardlink_exclusive_path_rejects_existing(
    root: FileSystemResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不支持硬链接的文件系统走回退分支时，也必须拒绝覆盖已存在文件。"""
    _run(root.create("doc.md", "原版"))
    real_link = os.link

    def no_link(src: str, dst: str, *, follow_symlinks: bool = True) -> None:
        # 用 95（不支持该操作）而不是 17：errno 17 会被 Python 映射成 FileExistsError，
        # 那样走的是正常"已存在"分支，测不到回退路径。
        raise OSError(95, "operation not supported")

    monkeypatch.setattr(os, "link", no_link)
    with pytest.raises(ResourceConflictError):
        _run(root.create("doc.md", "新版"))
    monkeypatch.setattr(os, "link", real_link)
    assert _run(root.read("doc.md")).text == "原版"


def test_successful_writes_leave_no_temp_files(root: FileSystemResources) -> None:
    _run(root.create("a.md", "1"))
    _run(root.patch("a.md", "2", expected_sha256=_run(root.read("a.md")).sha256))
    _run(root.create("nested/b.md", "3"))
    assert _leftover_tmp(root) == []


# ---------- 根目录本身 ----------


@pytest.mark.parametrize("bad_root", ["missing-dir", "file-as-root"])
def test_root_must_be_existing_directory(tmp_path: Path, bad_root: str) -> None:
    target = tmp_path / bad_root
    if bad_root == "file-as-root":
        target.write_text("x", encoding="utf-8")
    with pytest.raises(ResourceError):
        FileSystemResources(target)


def test_root_that_is_a_symlink_still_contains(tmp_path: Path) -> None:
    """root 自己可能是链接（如 macOS 的 /tmp）：归属判定必须按解析后的真实路径。"""
    real = tmp_path / "real-root"
    real.mkdir()
    provider = FileSystemResources(real)
    _run(provider.create("a.txt", "1"))
    assert (real / "a.txt").is_file()
    with pytest.raises(PathEscapeError):
        _run(provider.read("../etc/hosts"))


# ---------- 工具层 ----------


def test_capabilities_expose_four_tools(root: FileSystemResources) -> None:
    from yai_core import build_spec

    tools = filesystem_capabilities(root.root)
    assert [tool.__name__ for tool in tools] == [
        "file_list",
        "file_read",
        "file_create",
        "file_patch",
    ]
    for tool in tools:
        spec = build_spec(tool)
        assert spec.description, f"{tool.__name__} 缺 docstring"
        assert "path" in spec.input_schema["properties"]


def test_tools_run_through_executor(root: FileSystemResources) -> None:
    from yai_core import build_spec
    from yai_core.policy import AllowlistPolicy
    from yai_core.tools import ToolExecutor, ToolRegistry

    registry = ToolRegistry()
    for tool in filesystem_capabilities(root.root):
        registry.register(build_spec(tool))
    executor = ToolExecutor(registry, AllowlistPolicy((), mode="auto"), _NullChannel())

    events, ok, text = _run(executor.execute("file_create", {"path": "n.md", "content": "第一版"}))
    assert ok is True and "sha256" in text
    _events, ok2, text2 = _run(executor.execute("file_read", {"path": "n.md"}))
    assert ok2 is True and "第一版" in text2
    # 越权调用以失败回灌，而不是抛异常打断任务
    _events2, ok3, text3 = _run(executor.execute("file_read", {"path": "../etc/passwd"}))
    assert ok3 is False          # 越权以失败回灌给模型，不抛异常打断任务
    assert "上跳" in text3 or "拒绝" in text3


class _NullChannel:
    async def emit(self, event) -> None:  # pragma: no cover
        return None

    async def ask(self, question: str) -> str:  # pragma: no cover
        return ""

    async def confirm(self, tool_name: str, arguments: dict) -> bool:  # pragma: no cover
        return True


def test_reparse_detection_handles_missing_file(root: FileSystemResources) -> None:
    from yai_core.batteries.filesystem import _is_reparse

    assert _is_reparse(root.root / "does-not-exist") is False


def test_stat_attributes_absent_on_posix(root: FileSystemResources) -> None:
    """普通文件在两个平台上都不该被判成链接。"""
    _run(root.create("plain.txt", "x"))
    info = (root.root / "plain.txt").lstat()
    assert not stat_module.S_ISLNK(info.st_mode)


# ---------- 补齐三条实现分支 ----------


def test_all_four_tools_roundtrip(root: FileSystemResources) -> None:
    """四个工具都从工具层走一遍（它们才是宿主实际注册的东西）。"""
    file_list, file_read, file_create, file_patch = filesystem_capabilities(root.root)

    async def main() -> None:
        created = await file_create(path="docs/a.md", content="第一版")
        assert created["created"] is True and created["path"] == "docs/a.md"

        listed = await file_list("docs")
        assert listed["count"] == 1 and listed["entries"][0]["name"] == "a.md"
        assert listed["truncated"] is False

        read = await file_read(path="docs/a.md")
        assert read["content"] == "第一版"

        patched = await file_patch(
            path="docs/a.md", content="第二版", expected_sha256=read["sha256"]
        )
        assert patched["updated"] is True
        assert (await file_read(path="docs/a.md"))["content"] == "第二版"

        # 根目录列举 + 空参数默认值
        assert (await file_list())["path"] == "/"

    _run(main())


def test_reparse_helper_detects_symlink(root: FileSystemResources, tmp_path: Path) -> None:
    from yai_core.batteries.filesystem import _is_reparse

    outside = tmp_path / "victim2.txt"
    outside.write_text("x", encoding="utf-8")
    link = root.root / "a-link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无符号链接权限")
    assert _is_reparse(link) is True
    assert _is_reparse(root.root / "plain-missing.txt") is False


def test_discard_swallows_unlink_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from yai_core.batteries.filesystem import _discard

    def boom(name: str) -> None:
        raise OSError("临时文件已经没了")

    monkeypatch.setattr(os, "unlink", boom)
    _discard("whatever.tmp")          # 清理失败不能把主流程也带崩


def test_create_works_without_hardlink_support(
    root: FileSystemResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不支持硬链接的文件系统走回退分支时，新建仍要成功、且不留下临时文件。"""
    def no_link(src: str, dst: str, *, follow_symlinks: bool = True) -> None:
        raise OSError(95, "operation not supported")

    monkeypatch.setattr(os, "link", no_link)
    created = _run(root.create("fallback.txt", "内容"))
    assert created.sha256 == sha256_text("内容")
    assert _run(root.read("fallback.txt")).text == "内容"
    assert _leftover_tmp(root) == []
