#!/usr/bin/env python3
"""Build subsetted JetBrains Mono fonts for fully-offline embedding.

Source : JetBrains Mono v2.304, SIL Open Font License 1.1
         https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip
License: see shell/desktop/fonts/OFL.txt — that file MUST ship alongside the fonts.

The full upstream TTFs are intentionally NOT committed (they cover every script
and are hundreds of KB each). To rebuild the subsetted artifacts, download the
zip above, unzip it, and run from the repository root:

    uv run --no-project --with fonttools --with brotli \
        python scripts/build_subset_fonts.py --src <unzipped>/fonts/ttf

Outputs (ASCII + technical symbols only; CJK is deliberately excluded so Chinese
text keeps rendering with the system sans face instead of a fake monospace CJK):

    shell/desktop/fonts/JetBrainsMono-Regular.ttf   (QML / desktop shell)
    shell/desktop/fonts/JetBrainsMono-Bold.ttf
    shell/desktop/fonts/JetBrainsMono-Regular.woff2 (HTML hosts, base64-inlined)
    shell/desktop/fonts/JetBrainsMono-Bold.woff2
"""

from __future__ import annotations

import argparse
from pathlib import Path

# Codepoints the terminal UI actually needs. Everything outside these ranges
# (notably CJK) is dropped and falls back to the platform sans font at runtime.
UNICODE_RANGES: list[tuple[int, int]] = [
    (0x0020, 0x007E),  # Basic Latin (printable ASCII)
    (0x00A0, 0x00FF),  # Latin-1: currency (¥, £…), signs, middle dot
    (0x2000, 0x206F),  # General Punctuation: – — ‘ ’ “ ” • … ‰
    (0x2190, 0x21FF),  # Arrows (→ ← ↑ ↓ …)
    (0x2500, 0x257F),  # Box Drawing (┌─┐│└┘ …)
    (0x25A0, 0x25FF),  # Geometric Shapes (■ ▸ ● ◆ …)
    (0x276F, 0x276F),  # ❯ heavy right angle quote (terminal prompt)
]

WEIGHTS: dict[str, str] = {"Regular": "Regular", "Bold": "Bold"}


def _unicodes() -> list[int]:
    points: list[int] = []
    for lo, hi in UNICODE_RANGES:
        points.extend(range(lo, hi + 1))
    return points


def _subset(src: Path, dst: Path, flavor: str | None) -> None:
    from fontTools import subset

    options = subset.Options()
    options.flavor = flavor
    options.desubroutinize = True
    options.layout_features = ["*"]  # keep ligatures / kerning (e.g. => ->)
    options.name_IDs = ["*"]
    options.name_legacy = True
    options.name_languages = ["*"]
    options.notdef_outline = True
    options.recalc_bounds = True
    # Keep PostScript glyph names in TTF (debugging); drop them in woff2 (size).
    options.glyph_names = flavor is None

    font = subset.load_font(str(src), options)
    maker = subset.Subsetter(options=options)
    maker.populate(unicodes=_unicodes())
    maker.subset(font)

    dst.parent.mkdir(parents=True, exist_ok=True)
    subset.save_font(font, str(dst), options)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src",
        required=True,
        help="directory containing the upstream JetBrainsMono-*.ttf files",
    )
    parser.add_argument(
        "--out",
        default="shell/desktop/fonts",
        help="output directory (default: shell/desktop/fonts)",
    )
    args = parser.parse_args()

    src_dir = Path(args.src)
    out_dir = Path(args.out)

    for weight in WEIGHTS:
        src = src_dir / f"JetBrainsMono-{weight}.ttf"
        if not src.exists():
            raise SystemExit(f"missing source font: {src}")
        for flavor, ext in ((None, "ttf"), ("woff2", "woff2")):
            dst = out_dir / f"JetBrainsMono-{weight}.{ext}"
            _subset(src, dst, flavor)
            print(f"OK  {dst}  {dst.stat().st_size} bytes")


if __name__ == "__main__":
    main()
