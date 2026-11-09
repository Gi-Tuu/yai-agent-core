"""文档站守卫：mkdocs 配置、nav 路径与站点内链接不漂移。

站点由 mkdocs 从 `docs/` 构建后发布到 GitHub Pages。这里只做**静态校验**，不依赖 mkdocs
（CI 里的 docs 工作流才真正构建）；校验三件事：

1. `mkdocs.yml` 存在且 `docs_dir: docs` / `site_dir: site`；
2. `nav` 里点名的每个 `.md` 都真实存在；
3. 被跟踪的 docs markdown 中，所有相对链接都落在 `docs/` 内——否则站点上必然 404
   （指向仓库根的链接必须写成绝对 GitHub URL）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CONFIG = ROOT / "mkdocs.yml"

LINK_RE = re.compile(r"\]\(([^)]+)\)")
NAV_ENTRY_RE = re.compile(r"^\s*-\s+[^:]+:\s+([^\s]+\.md)\s*$", re.MULTILINE)
NAV_START = "nav:\n"
NAV_END = "not_in_nav:"


def _docs_markdown() -> list[Path]:
    """被跟踪的 docs markdown；没有 git（解包后的 sdist）时退回遍历。"""
    if (ROOT / ".git").exists():
        completed = subprocess.run(
            ["git", "ls-files", "-z", "docs/*.md"], cwd=ROOT, capture_output=True
        )
        names = [
            item.decode("utf-8", "surrogateescape")
            for item in completed.stdout.split(b"\0")
            if item
        ]
        return sorted(ROOT / name for name in names)
    return sorted(DOCS.rglob("*.md"))


def test_mkdocs_config_points_at_docs() -> None:
    assert CONFIG.is_file(), "mkdocs.yml 不存在——文档站配置是发布链的一部分"
    text = CONFIG.read_text(encoding="utf-8")
    assert re.search(r"^docs_dir:\s*docs\s*$", text, flags=re.MULTILINE)
    assert re.search(r"^site_dir:\s*site\s*$", text, flags=re.MULTILINE)


def test_nav_targets_exist() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    nav_block = text.split(NAV_START, 1)[1].split(NAV_END, 1)[0]
    paths = NAV_ENTRY_RE.findall(nav_block)
    assert paths, "mkdocs nav 里没有解析到任何 .md 路径"
    missing = [path for path in paths if not (DOCS / path).is_file()]
    assert not missing, f"nav 指向不存在的文件：{missing}"


def test_tracked_docs_links_stay_inside_site() -> None:
    escapes: list[str] = []
    for md in _docs_markdown():
        for raw in LINK_RE.findall(md.read_text(encoding="utf-8")):
            target = raw.split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "/", "#")):
                continue
            resolved = (md.parent / target).resolve()
            try:
                resolved.relative_to(DOCS)
            except ValueError:
                escapes.append(f"{md.relative_to(ROOT).as_posix()} -> {target}")
    assert not escapes, (
        "以下文档链接指向 docs/ 之外，站点上会 404；请改成绝对 GitHub URL：\n"
        + "\n".join(escapes)
    )
