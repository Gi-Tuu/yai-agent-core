"""发出去的包自己得能用：sdist 内的**文档相对链接**必须全部可达。

背景（2026-10-25 实测）：v0.22.0 的 sdist 里，文档相对链接断链 16 处——
README 白皮书引用的 9 张赛事配图、部署文档引用的 competitions/deployment.md、
以及白皮书/plans 引用的 ROADMAP.md，全都不在包里。包是发出去了，但读者一翻就是断图死链。

本文件把这件事钉住：
1. 仓库内所有被跟踪 markdown 的相对链接，在**仓库**里必须可达；
2. sdist 白名单里包含的 markdown，其相对链接目标必须**也在白名单里**
   （否则包内必然断链）。

只做文件级校验，不校验 `#锚点`（中文标题的 slug 规则易变，误报成本高于收益）。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: hatchling 按 PEP 517 强制打进 sdist 的文件（不由 include 白名单决定）。
ALWAYS_IN_SDIST = frozenset({"pyproject.toml", "PKG-INFO"})

#: 匹配 markdown 里的相对链接（含锚点）。
LINK_RE = re.compile(r"\]\(([^)#][^)]*)\)")


def _sdist_patterns() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    raw = data["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    return [p.lstrip("/") for p in raw]


def _in_sdist(rel: str, patterns: list[str]) -> bool:
    """rel 是仓库相对路径（正斜杠）。判断它是否会被打进 sdist。

    hatchling 用 gitignore 语义编译 include；本项目白名单一律以 "/" 根锚定，
    因此"/X"要么是精确文件，要么是"目录及其下全部"。
    另外 hatchling 按 PEP 517 **强制**把 pyproject.toml 与 PKG-INFO 打进 sdist，
    不由 include 白名单决定。
    """
    if rel in ALWAYS_IN_SDIST:
        return True
    for pat in patterns:
        if rel == pat or rel.startswith(pat.rstrip("/") + "/"):
            return True
    return False


def _md_files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*.md"):
        rel = p.relative_to(ROOT)
        parts = rel.parts
        if any(x in parts for x in (".venv", "node_modules", "docs/exports", "models")):
            continue
        out.append(p)
    return sorted(out)


def _link_targets(md: Path) -> list[str]:
    """取出相对链接目标（剥掉 ``#锚点``——锚点不校验，中文标题 slug 易变）。"""
    text = md.read_text(encoding="utf-8", errors="replace")
    out = []
    for raw in LINK_RE.findall(text):
        target = raw.split(" ")[0].strip("<>").split("#", 1)[0]
        if not target:
            continue
        if target.startswith(("http://", "https://", "mailto:", "data:")):
            continue
        out.append(target)
    return out


def test_repository_relative_links_resolve() -> None:
    """仓库内所有被跟踪 markdown 的相对链接必须可达。"""
    broken: list[str] = []
    for md in _md_files():
        for target in _link_targets(md):
            if not (md.parent / target).exists():
                broken.append(f"{md.relative_to(ROOT).as_posix()} -> {target}")
    assert not broken, "仓库内存在断链：\n" + "\n".join(broken)


def test_sdist_internal_links_resolve() -> None:
    """进了包的文档，其相对链接目标也必须在包里。"""
    patterns = _sdist_patterns()
    broken: list[str] = []
    for md in _md_files():
        rel_md = md.relative_to(ROOT).as_posix()
        if not _in_sdist(rel_md, patterns):
            continue  # 这份文档本身不在包里，它断链不影响使用者
        for target in _link_targets(md):
            resolved = (md.parent / target).resolve()
            try:
                rel_target = resolved.relative_to(ROOT).as_posix()
            except ValueError:
                broken.append(f"{rel_md} -> {target}（指向仓库外）")
                continue
            if not (ROOT / rel_target).exists():
                broken.append(f"{rel_md} -> {target}（目标不在仓库里）")
            elif not _in_sdist(rel_target, patterns):
                broken.append(f"{rel_md} -> {target}（目标没进 sdist，包内会断链）")
    assert not broken, "sdist 内存在断链：\n" + "\n".join(broken)


@pytest.mark.parametrize(
    "target",
    [
        "README.md",
        "README.en.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "CONTRIBUTING.en.md",
        "SECURITY.md",
        "SECURITY.zh-CN.md",
        "ROADMAP.md",
        "LICENSE",
        "uv.lock",
        ".env.example",
        "docs/architecture.md",
        "docs/whitepaper.md",
        "docs/walkthrough/00-index.md",
        "docs/releases/v0.22.0.md",
        "docs/competitions/deployment.md",
        "docs/competitions/shanghai/figs/fig1_position.png",
    ],
)
def test_key_documents_are_in_sdist(target: str) -> None:
    """这些文件是包内可达性的支点，缺一个就可能让别人的文档断链。"""
    patterns = _sdist_patterns()
    assert _in_sdist(target, patterns), f"{target} 不在 sdist 白名单里"
    assert (ROOT / target).exists(), f"{target} 在仓库里不存在"
