"""SECURITY 中英两份的联系渠道必须一致。

项目约定门面文档双语同步（README / CONTRIBUTING / SECURITY 各有两份）。
这次修安全联系邮箱时发现两份同时带着同一个错地址——说明**没有东西在盯**：
只改一份、或两处都抄错，CI 都不会红。这里钉一条最小断言：两份文件里出现的
邮箱必须完全相同，且必须真的存在。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SECURITY_FILES = ("SECURITY.md", "SECURITY.zh-CN.md")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: 两份文件的小节标题语言不同，取"报告漏洞 / Reporting a vulnerability"之后的部分；
#: 徽章链接与文档其他位置的地址不参与比对。
_HEADINGS = ("## 报告漏洞", "## Reporting a vulnerability")


def _emails_in(name: str) -> set[str]:
    text = (ROOT / name).read_text(encoding="utf-8")
    for heading in _HEADINGS:
        if heading in text:
            text = text.split(heading, 1)[1]
    return {match.group(0).lower() for match in _EMAIL.finditer(text)}


def test_both_security_files_exist() -> None:
    for name in SECURITY_FILES:
        assert (ROOT / name).is_file(), f"缺少 {name}"


def test_reporting_contact_is_present_and_identical() -> None:
    per_file = {name: _emails_in(name) for name in SECURITY_FILES}
    for name, emails in per_file.items():
        assert emails, f"{name} 的报告漏洞章节里没有邮箱——安全政策不能没有入口"
        assert len(emails) == 1, f"{name} 里出现多个联系邮箱：{sorted(emails)}"
    zh, en = (per_file[name] for name in SECURITY_FILES)
    assert zh == en, f"中英两份 SECURITY 的联系邮箱不一致：{sorted(zh)} vs {sorted(en)}"
