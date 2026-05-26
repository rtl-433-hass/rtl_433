#!/usr/bin/env python3
"""Generate Home Assistant brand assets for the rtl_433 integration.

rtl_433 (https://github.com/merbanan/rtl_433) has no official logo, so this
produces a simple, recognisable placeholder: a rounded "app tile" icon and a
wordmark logo built around an RF broadcast glyph "((•))" and the "rtl_433"
monospace wordmark.

Outputs (Home Assistant brands spec — square icon 256/512, logo shortest side
128-256 / 256-512, PNG, transparent, trimmed):
  brands/icon.png        256x256
  brands/icon@2x.png     512x512
  brands/logo.png        height 256, trimmed width
  brands/logo@2x.png     height 512, trimmed width

Run:  python brands/generate_brand_assets.py
Requires Pillow and the DejaVu Sans Mono Bold font.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# Palette: an RF/radio blue gradient that reads on both light and dark themes.
GRAD_TOP = (43, 179, 224)  # bright teal-blue
GRAD_BOTTOM = (11, 58, 119)  # deep navy
WHITE = (255, 255, 255)

WORDMARK = "rtl_433"


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _vgradient(w: int, h: int, top, bottom) -> Image.Image:
    """Vertical gradient as an RGB image (one filled row per scanline)."""
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        draw.line([(0, y), (w, y)], fill=_lerp(top, bottom, y / max(1, h - 1)))
    return img


def _fit_font(text: str, target_w: float) -> ImageFont.FreeTypeFont:
    """Return a font sized so ``text`` is about ``target_w`` pixels wide."""
    probe = ImageFont.truetype(FONT_PATH, 100)
    w = ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(text, font=probe)
    return ImageFont.truetype(FONT_PATH, max(1, int(100 * target_w / w)))


def _broadcast(draw: ImageDraw.ImageDraw, cx, cy, r0, n, width, fade=True):
    """Draw an RF "((•))" broadcast glyph centred at (cx, cy), white."""
    for i in range(n):
        r = r0 * (1 + i)
        bbox = [cx - r, cy - r, cx + r, cy + r]
        a = int(255 * (1 - 0.22 * i)) if fade else 255
        # right ")" arc and left "(" arc (PIL: 0deg=3 o'clock, clockwise)
        draw.arc(bbox, start=-52, end=52, fill=WHITE + (a,), width=width)
        draw.arc(bbox, start=128, end=232, fill=WHITE + (a,), width=width)
    dot = r0 * 0.42
    draw.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=WHITE + (255,))


def build_icon(master: int = 1024) -> Image.Image:
    """Square rounded-tile icon: broadcast glyph + 'rtl_433' wordmark."""
    s = master
    # Rounded gradient tile on a transparent square.
    grad = _vgradient(s, s, GRAD_TOP, GRAD_BOTTOM)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, s - 1, s - 1], radius=int(s * 0.225), fill=255
    )
    tile = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)

    draw = ImageDraw.Draw(tile)
    # Broadcast glyph in the upper third.
    _broadcast(
        draw,
        cx=s / 2,
        cy=s * 0.355,
        r0=s * 0.105,
        n=3,
        width=max(3, int(s * 0.028)),
    )
    # Wordmark across ~78% width, lower half.
    font = _fit_font(WORDMARK, s * 0.78)
    box = draw.textbbox((0, 0), WORDMARK, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    draw.text(
        ((s - tw) / 2 - box[0], s * 0.70 - th / 2 - box[1]),
        WORDMARK,
        font=font,
        fill=WHITE + (255,),
    )
    return tile


def build_logo(master_h: int = 1024) -> Image.Image:
    """Wide wordmark logo: gradient-filled broadcast glyph + 'rtl_433'."""
    h = master_h
    glyph_cx = h * 0.52
    glyph_cy = h * 0.5
    r0 = h * 0.13
    gap = h * 0.18
    text_left = glyph_cx + r0 * 3 + gap

    font = ImageFont.truetype(FONT_PATH, int(h * 0.52))
    tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    box = tmp.textbbox((0, 0), WORDMARK, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    w = int(text_left + tw + h * 0.12)

    # Draw the shape (glyph + text) in white on a transparent layer, then pour
    # the blue gradient through its alpha so the wordmark works on light and
    # dark themes.
    shape_rgba = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shape_rgba)
    _broadcast(
        sdraw, glyph_cx, glyph_cy, r0, 3, width=max(3, int(h * 0.05)), fade=False
    )
    sdraw.text(
        (text_left - box[0], glyph_cy - th / 2 - box[1]),
        WORDMARK,
        font=font,
        fill=WHITE + (255,),
    )
    alpha = shape_rgba.split()[3]

    grad = _vgradient(w, h, GRAD_TOP, GRAD_BOTTOM).convert("RGBA")
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out = Image.composite(grad, out, alpha)
    return out.crop(out.getbbox())  # trim transparent margins


def _save(img: Image.Image, name: str, size) -> None:
    if isinstance(size, int):  # square
        target = (size, size)
    else:  # (height,) -> scale by height, keep aspect
        scale = size[0] / img.height
        target = (max(1, round(img.width * scale)), size[0])
    img.resize(target, Image.LANCZOS).save(HERE / name)
    print(f"wrote {name} ({target[0]}x{target[1]})")


def main() -> None:
    icon = build_icon()
    _save(icon, "icon.png", 256)
    _save(icon, "icon@2x.png", 512)

    logo = build_logo()
    _save(logo, "logo.png", (256,))
    _save(logo, "logo@2x.png", (512,))


if __name__ == "__main__":
    main()
