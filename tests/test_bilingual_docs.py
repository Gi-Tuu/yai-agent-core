"""双语文档守卫：国际面文档必须有英文版、两侧互相链接、且英文版进 nav。

口径见 `docs/BILINGUAL.md`：中文是权威版本，英文允许滞后；walkthrough 与内部过程稿不翻译。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CONFIG = ROOT / "mkdocs.yml"

#: 必须成对维护的"国际面"文档（相对 docs/ 的中文路径）。
REQUIRED_PAIRS = (
    "index.md",
    "IMPLEMENTATION_STATUS.md",
    "reading-guide.md",
    "architecture.md",
    "comparison.md",
    "guides/production-permissions.md",
    "guides/demo-deployment.md",
    "guides/browser-extension-bridge.md",
    "guides/tracing.md",
    "guides/multilang-interop.md",
    "guides/execution-audit-persistence.md",
    "guides/deployment-scaling.md",
    "guides/federation.md",
    "guides/openapi-31-compat.md",
    "evaluations/agentdojo-evaluation.md",
    "evaluations/browser-task-e2e.md",
    "evaluations/competitor-openmuse-audit.md",
    "evaluations/ecosystem-interop.md",
    "evaluations/memory-evaluation.md",
    "evaluations/openssf-best-practices-self-assessment.md",
    "evaluations/sidepanel-manual-acceptance.md",
    "evaluations/windows-real-apps.md",
    "adr/README.md",
    "adr/0002-runtime-auth-boundary.md",
    "adr/0003-acc-boundary.md",
    "adr/0004-browser-provider-boundary.md",
    "adr/0005-outcome-verification.md",
    "adr/0006-browser-execution-unification.md",
    "adr/0007-local-secret-storage.md",
    "adr/0008-directed-federation.md",
)
NAV_START = "nav:\n"
NAV_END = "not_in_nav:"


def _english_path(chinese: Path) -> Path:
    return chinese.with_name(chinese.stem + ".en.md")


def _nav_text() -> str:
    text = CONFIG.read_text(encoding="utf-8")
    return text.split(NAV_START, 1)[1].split(NAV_END, 1)[0]


def _tracked_english_docs() -> list[Path]:
    if (ROOT / ".git").exists():
        completed = subprocess.run(
            ["git", "ls-files", "-z", "docs/*.en.md", "docs/**/*.en.md"],
            cwd=ROOT,
            capture_output=True,
        )
        return sorted(
            ROOT / item.decode("utf-8", "surrogateescape")
            for item in completed.stdout.split(b"\0")
            if item
        )
    return sorted(DOCS.rglob("*.en.md"))


def test_required_pairs_have_english_counterpart() -> None:
    missing = [
        chinese for chinese in REQUIRED_PAIRS if not _english_path(DOCS / chinese).is_file()
    ]
    assert not missing, f"缺少英文版（应放在同名 .en.md）：{missing}"


def test_pairs_link_to_each_other_and_state_canonical() -> None:
    problems: list[str] = []
    for chinese in REQUIRED_PAIRS:
        zh = DOCS / chinese
        en = _english_path(zh)
        if not en.is_file():
            continue
        zh_text = zh.read_text(encoding="utf-8")
        en_text = en.read_text(encoding="utf-8")
        en_name = en.name
        if en_name not in zh_text:
            problems.append(f"{chinese} 顶部没有指向 {en_name} 的 English 链接")
        if zh.name not in en_text:
            problems.append(f"{en_name} 顶部没有指回 {zh.name} 的链接")
        head = en_text[:1200]
        if "English translation" not in head:
            problems.append(f"{en_name} 顶部缺少 English translation 说明")
        if "canonical" not in head:
            problems.append(f"{en_name} 顶部缺少 canonical（中文权威）说明")
    assert not problems, "\n".join(problems)


def test_english_docs_are_in_nav() -> None:
    nav = _nav_text()
    missing = [
        path.relative_to(DOCS).as_posix()
        for path in _tracked_english_docs()
        if path.relative_to(DOCS).as_posix() not in nav
    ]
    assert not missing, f"英文文档未进 mkdocs English 导航：{missing}"


def test_english_docs_start_with_translation_notice() -> None:
    bad: list[str] = []
    for path in _tracked_english_docs():
        head = path.read_text(encoding="utf-8")[:1200]
        if not re.search(r">\s*English translation of \[中文\]", head):
            bad.append(path.relative_to(ROOT).as_posix())
    assert not bad, f"英文文档缺少统一翻译声明：{bad}"
