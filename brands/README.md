# Brand assets

Home Assistant brand icon/logo for the `rtl_433` integration.

> **Placeholder.** rtl_433 ([merbanan/rtl_433](https://github.com/merbanan/rtl_433))
> has no official logo, so these are generated placeholders: an RF broadcast
> glyph `((•))` with the `rtl_433` monospace wordmark on a radio-blue gradient.
> Replace them if an official mark ever exists.

## Files

The generated images live in
[`custom_components/rtl_433/brand/`](../custom_components/rtl_433/brand/) — the
location Home Assistant 2026.3+ serves directly:

| File | Size | Purpose |
|------|------|---------|
| `brand/icon.png` | 256×256 | Square avatar icon |
| `brand/icon@2x.png` | 512×512 | hDPI icon |
| `brand/logo.png` | 1297×256 | Wordmark logo |
| `brand/logo@2x.png` | 2593×512 | hDPI logo |

All are PNG, transparent, and trimmed, per the
[Home Assistant brands](https://github.com/home-assistant/brands) requirements
(square icon 256/512; logo shortest side 128–256 / 256–512).

## Regenerating

```bash
uv pip install pillow         # or: pip install pillow
python brands/generate_brand_assets.py   # writes to custom_components/rtl_433/brand/
```

Requires the DejaVu Sans Mono Bold font
(`/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf` on Debian/Ubuntu).

## How Home Assistant serves the icon

1. **In-repo brand images (HA 2026.3.0+)** — the primary mechanism used here.
   Home Assistant loads `custom_components/rtl_433/brand/{icon,logo}.png`
   (and `@2x`) directly and they take priority over the brands CDN, with no
   extra configuration (see the
   [Brands Proxy API announcement](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api)).
2. **`home-assistant/brands` submission** (covers HA < 2026.3): open a PR adding
   the same four files under `custom_integrations/rtl_433/`. Older Home
   Assistant fetches integration icons from `https://brands.home-assistant.io/`.

On Home Assistant versions older than 2026.3 without a brands submission, a
generic placeholder icon is shown — this does not affect functionality.
