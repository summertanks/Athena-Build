#!/usr/bin/env python3
# regenerate-bg.py — produce installer/boot/grub-background.png
#
# Output is a 800x600 GRUB-compatible PNG that mirrors the Aegis visual
# identity from fork/source/athena-branding/data/aegis-dark.svg —
# midnight-indigo radial-gradient sky, gold-stroked Greek capital alpha
# (Α) at the optical centre.
#
# The PNG is committed to the repo (binary blob) so the build doesn't
# depend on PIL / ImageMagick / librsvg at runtime; this script is the
# single source of truth when the asset needs a refresh (palette change,
# new resolution, etc.).
#
# Usage:
#   python3 installer/boot/regenerate-bg.py
#
# Rendered at 800x600 because GRUB's gfxmode defaults to that and most
# bootloaders pick it cleanly; the image looks fine at 1024x768 too if
# the operator's firmware advertises that mode.
#
# Per docs/branding-methodology.md Pattern B (drop-in static asset
# owned by us) — no upstream patches, no per-version churn.

from PIL import Image
import math
import os

W, H = 800, 600
OUT = os.path.join(os.path.dirname(__file__), 'grub-background.png')


def _radial_gradient(w, h, cx, cy, stops):
    """Render a radial gradient.  `stops` is [(offset, (r,g,b)), ...]
    where offset is 0..1 from center to corner."""
    img = Image.new('RGB', (w, h))
    px = img.load()
    _max_r = math.hypot(max(cx, w - cx), max(cy, h - cy))
    for y in range(h):
        for x in range(w):
            _t = min(1.0, math.hypot(x - cx, y - cy) / _max_r)
            # find segment
            for i in range(len(stops) - 1):
                _o0, _c0 = stops[i]
                _o1, _c1 = stops[i + 1]
                if _o0 <= _t <= _o1:
                    _f = (_t - _o0) / (_o1 - _o0) if _o1 > _o0 else 0
                    px[x, y] = tuple(
                        int(_c0[k] + (_c1[k] - _c0[k]) * _f) for k in range(3)
                    )
                    break
            else:
                px[x, y] = stops[-1][1]
    return img


def _draw_alpha_glyph(img, cx, cy, height, stroke_top, stroke_bot, width):
    """Draw a stylised Greek capital Α (alpha) with vertical gold-
    gradient stroke.  Geometry: isoceles triangle missing its base,
    with a horizontal cross-bar at ~60% height.  Mirrors the SVG path
    in aegis-dark.svg's <path id="alpha">.  No upstream font dep —
    drawn as lines for predictable kerning."""
    _half_w = int(height * 0.42)
    _top  = (cx,           cy - height // 2)
    _bl   = (cx - _half_w, cy + height // 2)
    _br   = (cx + _half_w, cy + height // 2)
    # cross-bar inset 18% from each leg's interior
    _t = 0.6
    _cl_x = int(_bl[0] + (_top[0] - _bl[0]) * _t + _half_w * 0.10)
    _cl_y = int(_bl[1] + (_top[1] - _bl[1]) * _t)
    _cr_x = int(_br[0] + (_top[0] - _br[0]) * _t - _half_w * 0.10)
    _cr_y = int(_br[1] + (_top[1] - _br[1]) * _t)

    # Vertical gradient on the glyph: top→bot interpolation per y-row.
    # Render each segment by stepping along the line.
    def _grad_line(p0, p1):
        _len = max(1, int(math.hypot(p1[0] - p0[0], p1[1] - p0[1])))
        for _step in range(_len + 1):
            _f  = _step / _len
            _x  = int(p0[0] + (p1[0] - p0[0]) * _f)
            _y  = int(p0[1] + (p1[1] - p0[1]) * _f)
            # interpolate top→bot colour by global Y position on the
            # glyph (not segment-local) for visual coherence across legs
            _gf = (_y - (cy - height // 2)) / max(1, height)
            _gf = max(0.0, min(1.0, _gf))
            _c  = tuple(
                int(stroke_top[k] + (stroke_bot[k] - stroke_top[k]) * _gf)
                for k in range(3)
            )
            # paint a square of side `width` centred on (_x, _y)
            for _dx in range(-width // 2, width // 2 + 1):
                for _dy in range(-width // 2, width // 2 + 1):
                    _px = _x + _dx
                    _py = _y + _dy
                    if 0 <= _px < img.size[0] and 0 <= _py < img.size[1]:
                        img.putpixel((_px, _py), _c)

    _grad_line(_top, _bl)
    _grad_line(_top, _br)
    _grad_line((_cl_x, _cl_y), (_cr_x, _cr_y))


def _sparse_stars(img, n=80, seed=1):
    """Sparse white star pixels in the upper half — matches the SVG's
    star field.  Deterministic seed so the PNG is reproducible."""
    import random
    rng = random.Random(seed)
    px = img.load()
    for _ in range(n):
        _x = rng.randint(0, img.size[0] - 1)
        _y = rng.randint(0, int(img.size[1] * 0.55))
        # vary brightness so the field isn't uniform
        _b = rng.randint(120, 235)
        px[_x, _y] = (_b, _b, _b)


def main() -> None:
    # Sky — radial gradient from upper-center, midnight indigo
    img = _radial_gradient(
        W, H, cx=W // 2, cy=int(H * 0.22),
        stops=[
            (0.00, (0x1f, 0x2c, 0x52)),
            (0.55, (0x0d, 0x15, 0x2e)),
            (1.00, (0x04, 0x07, 0x0f)),
        ],
    )
    # Sparse stars (upper half only — same as SVG)
    _sparse_stars(img, n=80)
    # Gold-stroked alpha glyph at the optical centre
    _draw_alpha_glyph(
        img,
        cx=W // 2, cy=int(H * 0.55),
        height=int(H * 0.42),
        stroke_top=(0xf0, 0xd2, 0x7b),   # bright gold (top)
        stroke_bot=(0xa4, 0x7a, 0x2b),   # darker gold (bottom)
        width=4,
    )
    # Save
    img.save(OUT, 'PNG', optimize=True)
    print(f'wrote {OUT} ({os.path.getsize(OUT)} bytes, {W}x{H})')


if __name__ == '__main__':
    main()
