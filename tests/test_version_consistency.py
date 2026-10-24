"""版本串一致性：`pyproject.toml` / `__version__` / `uv.lock` / **两份 README 徽章** 必须同值。

发版时要改的地方分散在三个文件里，而**漏改任何一处都不会让 CI 变红**：
打包元数据、运行时元数据与锁文件各说各话，用户 `pip install` 到的版本和
`yai_core.__version__` 报出来的版本可以差一个号。所以把它钉成断言。

2026-10 补：两份 README 顶部的版本徽章也纳入钉子。此前 `README.en.md` 的徽章
停在 v0.20.0、`README.md` 已是 v0.21.0——中英门面漂移，而 CI 全绿；
AGENTS.md 明确要求「改其中一份的事实性内容（版本号…）时必须同步另一份」。

只用标准库：`tomllib` 自 3.11 起随 Python 发行（本项目 `requires-python >=3.11`）。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yai_core

ROOT = Path(__file__).resolve().parents[1]

#: 顶部版本徽章必须出现在这两份门面文件里，且版本号必须与包版本同值。
README_FACADES = ("README.md", "README.en.md")


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


def _readme_badge_version(name: str) -> str | None:
    """取出 README 顶部 `badge/version-vX.Y.Z-blue.svg` 里的版本号。"""
    text = (ROOT / name).read_text(encoding="utf-8")
    match = re.search(r"badge/version-v([0-9]+\.[0-9]+\.[0-9]+)-", text)
    return match.group(1) if match else None


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


def test_readme_badges_match_package_version() -> None:
    """两份 README 的版本徽章必须与包版本同值（中英门面同步）。"""
    version = _pyproject_version()
    for name in README_FACADES:
        badge = _readme_badge_version(name)
        assert badge is not None, f"{name} 里找不到版本徽章"
        assert badge == version, (
            f"{name} 的版本徽章是 v{badge}，与包版本 v{version} 不一致——"
            "中英门面漂移；请同时改 README.md 与 README.en.md"
        )


def test_readme_does_not_claim_an_older_version_as_current() -> None:
    """README 的「版本路线」不得把旧版本标成「当前发布版本 / current release」。"""
    version = _pyproject_version()
    for name in README_FACADES:
        text = (ROOT / name).read_text(encoding="utf-8")
        for match in re.finditer(r"v(\d+\.\d+\.\d+)\s*[（(][^）)]*当前发布版本", text):
            claimed = match.group(1)
            assert claimed == version, (
                f"{name} 仍把 v{claimed} 标为「当前发布版本」，实际当前版本是 v{version}"
            )
        for match in re.finditer(r"v(\d+\.\d+\.\d+)\s*\([^)）]*current release", text):
            claimed = match.group(1)
            assert claimed == version, (
                f"{name} still marks v{claimed} as 'current release'; actual is v{version}"
            )


def test_changelog_latest_section_is_the_package_version() -> None:
    """CHANGELOG 的最新小节必须就是当前版本（防止"发了包但没写小节"）。"""
    version = _pyproject_version()
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = re.findall(r"^## \[v(\d+\.\d+\.\d+)\]", text, flags=re.M)
    assert sections, "CHANGELOG 里找不到任何版本小节"
    assert sections[0] == version, (
        f"CHANGELOG 最新小节是 v{sections[0]}，与包版本 v{version} 不一致"
    )
