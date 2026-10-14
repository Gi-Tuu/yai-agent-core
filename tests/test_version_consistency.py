"""版本串一致性：`pyproject.toml` / `__version__` / `uv.lock` 三处必须同值。

发版时要改的地方分散在三个文件里，而**漏改任何一处都不会让 CI 变红**：
打包元数据、运行时元数据与锁文件各说各话，用户 `pip install` 到的版本和
`yai_core.__version__` 报出来的版本可以差一个号。所以把它钉成断言。

只用标准库：`tomllib` 自 3.11 起随 Python 发行（本项目 `requires-python >=3.11`）。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yai_core

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _lock_version() -> str:
    """从 uv.lock 里取本包自己的版本（不是依赖的同名条目）。"""
    text = (ROOT / "uv.lock").read_text(encoding="utf-8")
    match = re.search(
        r'\[\[package\]\]\s*\nname = "yai-agent-core"\s*\nversion = "([^"]+)"',
        text,
    )
    assert match is not None, "uv.lock 里找不到本包条目"
    return match.group(1)


def test_runtime_version_matches_pyproject() -> None:
    assert yai_core.__version__ == _pyproject_version()


def test_lockfile_matches_pyproject() -> None:
    assert _lock_version() == _pyproject_version()


def test_version_is_semver_and_matches_changelog() -> None:
    version = yai_core.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"非法版本号：{version}"
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [v{version}]" in changelog, (
        f"CHANGELOG 里没有 v{version} 小节：发版要么忘了改标题，"
        "要么版本串与已发布内容对不上"
    )
