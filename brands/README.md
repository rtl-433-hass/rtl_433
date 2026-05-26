# Brand assets

Home Assistant brand icon/logo for the `rtl_433` integration.

> **Placeholder.** rtl_433 ([merbanan/rtl_433](https://github.com/merbanan/rtl_433))
> has no official logo, so these are generated placeholders: an RF broadcast
> glyph `((•))` with the `rtl_433` monospace wordmark on a radio-blue gradient.
> Replace them if an official mark ever exists.

## Files

| File | Size | Purpose |
|------|------|---------|
| `icon.png` | 256×256 | Square avatar icon |
| `icon@2x.png` | 512×512 | hDPI icon |
| `logo.png` | 1297×256 | Wordmark logo |
| `logo@2x.png` | 2593×512 | hDPI logo |

All are PNG, transparent, and trimmed, per the
[Home Assistant brands](https://github.com/home-assistant/brands) requirements
(square icon 256/512; logo shortest side 128–256 / 256–512).

## Regenerating

```bash
uv pip install pillow         # or: pip install pillow
python brands/generate_brand_assets.py
```

Requires the DejaVu Sans Mono Bold font
(`/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf` on Debian/Ubuntu).

## Publishing the icon to Home Assistant

Home Assistant serves integration icons from
`https://brands.home-assistant.io/`, not from this repository's `manifest.json`.
There are two routes:

1. **Submit to `home-assistant/brands`** (works on all HA versions): open a PR
   adding these files under `custom_integrations/rtl_433/`
   (`icon.png`, `icon@2x.png`, `logo.png`, `logo@2x.png`).
2. **In-repo brands (HA 2026.3.0+)**: newer Home Assistant can resolve brand
   images shipped by the custom component via the
   [Brands Proxy API](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api).

Until the icon is published, Home Assistant shows a generic placeholder for the
integration — this does not affect functionality.
