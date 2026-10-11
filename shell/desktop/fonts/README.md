# Bundled font: JetBrains Mono (subsetted)

The Terminal Craft UI uses one engineering mono face. To stay **fully offline**
(no CDN / npm / Google Fonts at runtime), a subsetted JetBrains Mono is bundled
in this directory.

- **Upstream**: JetBrains Mono **v2.304** — <https://github.com/JetBrains/JetBrainsMono>
- **License**: SIL Open Font License 1.1; see [`OFL.txt`](OFL.txt). The font is
  free to bundle and subset, and must travel with this license text. It must not
  be sold on its own.
- **Maintainer**: Gi-Tuu

## Files

| File | Used by |
| --- | --- |
| `JetBrainsMono-Regular.ttf`, `JetBrainsMono-Bold.ttf` | QML desktop shell (loads the local file directly) |
| `JetBrainsMono-Regular.woff2`, `JetBrainsMono-Bold.woff2` | Single-file HTML hosts (base64-inlined into `index.html`) |

## What was subsetted

Only the codepoints the terminal UI renders in monospace:

- `U+0020–007E` printable ASCII
- `U+00A0–00FF` Latin-1 (currency incl. `¥ £`, signs, `·`)
- `U+2000–206F` general punctuation (`– — ‘ ’ “ ” • …`)
- `U+2190–21FF` arrows (`→ ← ↑ ↓`)
- `U+2500–257F` box drawing (`┌ ─ ┐ │ └ ┘`)
- `U+25A0–25FF` geometric shapes (`■ ▸ ● ◆`)
- `U+276F` terminal prompt `❯`

**CJK is intentionally excluded.** Chinese prose keeps using the system sans
face (`Segoe UI / PingFang SC / Microsoft YaHei / Noto Sans CJK`); forcing a
monospace CJK fallback would look broken. Chinese inside mono blocks (JSON values,
event reasons) falls back per-glyph to the system sans face.

## Rebuilding

The full upstream TTFs (hundreds of KB, every script) are not committed. Download
the release zip, then from the repository root:

```powershell
# 1. subset (needs fonttools + brotli only for this step)
uv run --no-project --with fonttools --with brotli `
  python scripts/build_subset_fonts.py --src <unzipped>/fonts/ttf

# 2. inline the woff2 into each single-file HTML host (stdlib only)
uv run --no-project python scripts/inline_mono_font.py `
  examples/host_e_sales_crm/web/index.html
```

The QML shell references the `.ttf` files in this directory directly; no inlining
step is needed there.
