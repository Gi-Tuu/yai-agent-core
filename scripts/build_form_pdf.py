"""把"栏目式" markdown 渲染成 A4 表单 PDF（表格框线 + 标签列底纹）。

与 `build_docx.py` 的分工：那个出"白皮书/长文"（封面 + 目录 + 章节另起页），
本脚本出"表单"——栏目即表格行，一页一屏，不带封面与目录。

用法：
  .venv/Scripts/python.exe scripts/build_form_pdf.py \\
      docs/examples/form.md docs/exports/form.pdf

markdown 约定（刻意宽松，只吃三种结构）：
- `#` 一级标题 → 表头大标题（居中）；`##` → 分节标题（居中）；
- 管道表格 → 带框线表格；**偶数列**（第 1、3 列…）视为标签格，加粗 + 灰底；
  四列表按 13/37/13/37 分宽，两列表按 15/85；
- 单元格内允许 `**粗体**`、`` `代码` `` 与 `<br>`（换行），其余按纯文本转义。

为什么走 HTML → LibreOffice 而不是 python-docx：这类表单要的是"网格 + 底纹 + 中文正文"，
HTML 路线零新增依赖（不装 python-docx），且 @page 直接控制 A4 页边距。
注意 LibreOffice 的 Writer/Web 导入会忽略 CSS 的 border-collapse 与 background，
因此边框/底纹写成传统 `border` / `background-color` 内联属性，否则导出后线全丢。
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CSS = """
@page { size: A4; margin: 14mm 12mm; }
body { font-family: "Source Han Serif SC","Noto Serif CJK SC","SimSun",serif;
       font-size: 9.5pt; line-height: 1.5; color: #111; }
h1 { font-size: 16pt; text-align: center; margin: 0 0 8pt; letter-spacing: 2pt; }
h2 { font-size: 11pt; text-align: center; margin: 10pt 0 3pt; letter-spacing: 1pt; }
table { width: 100%; table-layout: fixed; }
p { margin: 0 0 4pt; }
code { font-family: "Consolas","DejaVu Sans Mono",monospace; font-size: 8.5pt; }
"""

SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/opt/homebrew/bin/soffice",
)


def inline(text: str) -> str:
    """转义后只恢复三种记号：粗体、行内代码、`<br>`。"""
    esc = html.escape(text.strip())
    esc = esc.replace("&lt;br&gt;", "<br>")
    esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
    esc = re.sub(r"`([^`]+?)`", r"<code>\1</code>", esc)
    return esc


def table_html(block: list[str]) -> str:
    rows: list[list[str]] = []
    for raw in block:
        cells = [c.strip() for c in raw.strip().strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue  # markdown 分隔行
        rows.append(cells)
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    widths = ("13%", "37%", "13%", "37%") if ncols == 4 else ("15%", "85%")
    out = [
        '<table border="1" cellspacing="0" cellpadding="5" width="100%" '
        'style="border:1px solid #000;">',
        "<colgroup>"
        + "".join(f'<col width="{w}">' for w in widths[:ncols])
        + "</colgroup>",
    ]
    for row in rows:
        cells = []
        for idx, cell in enumerate(row):
            style = "border:1px solid #000;"
            if idx % 2 == 0:  # 偶数列 = 标签格
                style += "background-color:#efefef;font-weight:bold;"
            cells.append(f'<td valign="top" style="{style}">{inline(cell)}</td>')
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def render(md: str) -> str:
    parts: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("|"):
            block: list[str] = []
            while i < len(lines) and lines[i].rstrip().startswith("|"):
                block.append(lines[i].rstrip())
                i += 1
            parts.append(table_html(block))
            continue
        if line.startswith("## "):
            parts.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            parts.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                items.append(f"<li>{inline(lines[i].lstrip()[2:])}</li>")
                i += 1
            parts.append("<ul>" + "".join(items) + "</ul>")
            i += 1
            continue
        elif line:
            parts.append(f"<p>{inline(line)}</p>")
        i += 1
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{CSS}</style></head><body>" + "\n".join(parts) + "</body></html>"
    )


def find_soffice() -> str | None:
    found = shutil.which("soffice")
    if found:
        return found
    for candidate in SOFFICE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("markdown", type=Path)
    parser.add_argument("pdf", type=Path)
    args = parser.parse_args()

    soffice = find_soffice()
    if soffice is None:
        print("未找到 LibreOffice（soffice），无法导出 PDF", file=sys.stderr)
        return 1

    html_text = render(args.markdown.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="yai-form-") as tmp:
        interim = Path(tmp) / f"{args.pdf.stem}.html"
        interim.write_text(html_text, encoding="utf-8")
        subprocess.run(  # noqa: S603 - 固定可执行文件与参数，输入是本仓库自有文件
            [
                soffice,
                "--headless",
                "--convert-to",
                "pdf:writer_pdf_Export",
                "--outdir",
                str(args.pdf.parent),
                str(interim),
            ],
            check=True,
            capture_output=True,
        )
    size = args.pdf.stat().st_size if args.pdf.exists() else 0
    if not size:
        print("LibreOffice 未产出 PDF", file=sys.stderr)
        return 1
    print(f"{args.markdown.name} → {args.pdf.name}（{size // 1024} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
