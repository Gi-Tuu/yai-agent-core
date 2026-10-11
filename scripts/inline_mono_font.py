#!/usr/bin/env python3
"""Inline the subsetted JetBrains Mono woff2 files into a self-contained HTML host.

The demo web hosts are single-file apps served by a stdlib HTTP server that has
no static-file route, so the font is embedded as a base64 data URI (nothing is
fetched at runtime). Run after scripts/build_subset_fonts.py:

    uv run --no-project python scripts/inline_mono_font.py \
        examples/host_e_sales_crm/web/index.html

Idempotent: re-running replaces the previously injected <style> block and leaves
the rest of the file untouched.
"""

from __future__ import annotations

import argparse
import base64
import re
from pathlib import Path

FAMILY = "JBMono"
START = "/* JBMONO_FONT:START */"
END = "/* JBMONO_FONT:END */"

_BLOCK_RE = re.compile(
    r"\s*<style>\s*/\* JBMONO_FONT:START \*/.*?/\* JBMONO_FONT:END \*/\s*</style>",
    re.DOTALL,
)


def _face(weight: int, path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return (
        f"@font-face{{font-family:'{FAMILY}';font-weight:{weight};"
        f"font-style:normal;font-display:swap;"
        f"src:url(data:font/woff2;base64,{data}) format('woff2');}}"
    )


def _style_block(fonts: Path) -> str:
    regular = _face(400, fonts / "JetBrainsMono-Regular.woff2")
    bold = _face(700, fonts / "JetBrainsMono-Bold.woff2")
    return (
        "<style>\n"
        f"{START}\n"
        "/* Subsetted JetBrains Mono v2.304 (SIL OFL 1.1), inlined for offline use.\n"
        "   CJK excluded -> Chinese falls back to system sans. Rebuild via\n"
        "   scripts/build_subset_fonts.py + scripts/inline_mono_font.py. */\n"
        f"{regular}\n{bold}\n"
        f"{END}\n"
        "</style>"
    )


def inject(html_path: Path, fonts: Path) -> None:
    text = html_path.read_text(encoding="utf-8")
    block = _style_block(fonts)

    if _BLOCK_RE.search(text):
        text = _BLOCK_RE.sub("\n  " + block, text, count=1)
    else:
        if "</head>" not in text:
            raise SystemExit(f"no </head> found in {html_path}")
        text = text.replace("</head>", "  " + block + "\n</head>", 1)

    already = re.compile(r"--font-mono:\s*'" + FAMILY + r"'")
    if not already.search(text):
        cascade = re.compile(r"(--font-mono:\s*)'Cascadia Code'")
        if not cascade.search(text):
            raise SystemExit(f"could not find the --font-mono stack in {html_path}")
        text = cascade.sub(r"\1'" + FAMILY + r"','Cascadia Code'", text, count=1)

    html_path.write_text(text, encoding="utf-8")
    print(f"inlined -> {html_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", nargs="+", help="HTML file(s) to update")
    parser.add_argument("--fonts", default="shell/desktop/fonts")
    args = parser.parse_args()

    fonts = Path(args.fonts)
    for name in args.html:
        inject(Path(name), fonts)


if __name__ == "__main__":
    main()
