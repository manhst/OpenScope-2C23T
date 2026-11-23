#!/usr/bin/env python3
"""Generate src/ui_brand.h: the Fixfast logo for the multimeter screen.

The source is tools/brand/fixfast-logo.svg (a 64x64 rounded tile with a
lightning bolt, then the "Fixfast" wordmark). The tile itself is drawn by
gfx_round_rect() in its own colour, so only two shapes are stored, each as
the single glyph of a tiny font that gfx_text() draws like any other text:

  font_brand_bolt / BRAND_BOLT  the bolt, positioned inside a TILE x TILE tile
  font_brand_word / BRAND_WORD  the wordmark, scaled to WORD_W pixels wide

Both use the 4-bit alpha RLE format of tools/gen_fonts.py, so the logo costs
well under a kilobyte of flash and no extra drawing code.

The SVG paths are rasterised here (M/L/H/V/Q/Z, absolute and relative) with
8x8 supersampling and an even-odd fill, so no SVG library is needed.

Usage:
    python3 tools/gen_brand.py tools/brand/fixfast-logo.svg TILE WORD_W > src/ui_brand.h
    python3 tools/gen_brand.py tools/brand/fixfast-logo.svg 64 64 > src/ui_brand.h
"""

import math
import re
import sys

from PIL import Image, ImageChops, ImageDraw

from gen_fonts import rle

SS = 8  # supersampling factor
TILE_RADIUS = 16 / 64  # rx="16" on the 64-unit tile


def tokens(d):
    return re.findall(r"[MLHVQZmlhvqz]|-?(?:\d+\.?\d*|\.\d+)", d)


def subpaths(d, steps=8):
    """Flatten an SVG path into closed polygons (lists of (x, y))."""
    out, cur = [], []
    x = y = 0.0
    cmd = None
    t = tokens(d)
    i = 0

    def num():
        nonlocal i
        v = float(t[i])
        i += 1
        return v

    while i < len(t):
        if re.match(r"[A-Za-z]", t[i]):
            cmd = t[i]
            i += 1
            if cmd in "Zz":
                if cur:
                    out.append(cur)
                cur = []
                continue
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            nx, ny = num(), num()
            if rel:
                nx, ny = x + nx, y + ny
            if cur:
                out.append(cur)
            cur = [(nx, ny)]
            x, y = nx, ny
            cmd = "l" if rel else "L"  # implicit lineto after moveto
        elif c == "L":
            nx, ny = num(), num()
            if rel:
                nx, ny = x + nx, y + ny
            x, y = nx, ny
            cur.append((x, y))
        elif c == "H":
            nx = num()
            x = x + nx if rel else nx
            cur.append((x, y))
        elif c == "V":
            ny = num()
            y = y + ny if rel else ny
            cur.append((x, y))
        elif c == "Q":
            cx, cy, nx, ny = num(), num(), num(), num()
            if rel:
                cx, cy, nx, ny = x + cx, y + cy, x + nx, y + ny
            for k in range(1, steps + 1):
                s = k / steps
                px = (1 - s) ** 2 * x + 2 * (1 - s) * s * cx + s * s * nx
                py = (1 - s) ** 2 * y + 2 * (1 - s) * s * cy + s * s * ny
                cur.append((px, py))
            x, y = nx, ny
        else:
            raise ValueError(f"unsupported path command {cmd}")
    if cur:
        out.append(cur)
    return out


def raster(polys, size_w, size_h):
    """Even-odd fill at SSx, box-filtered down to 4-bit alpha rows."""
    big = Image.new("1", (size_w * SS, size_h * SS), 0)
    for poly in polys:
        layer = Image.new("1", big.size, 0)
        ImageDraw.Draw(layer).polygon([(px * SS, py * SS) for px, py in poly], fill=1)
        big = ImageChops.logical_xor(big, layer)
    small = big.convert("L").resize((size_w, size_h), Image.BOX)
    return [[(small.getpixel((x, y)) + 8) // 17 for x in range(size_w)] for y in range(size_h)]


def glyph(alpha, cp, advance_min):
    h, w = len(alpha), len(alpha[0])
    rows = [y for y in range(h) if any(alpha[y])]
    cols = [x for x in range(w) if any(alpha[y][x] for y in range(h))]
    y0, y1, x0, x1 = rows[0], rows[-1], cols[0], cols[-1]
    a4 = [alpha[y][x] for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    return (cp, x1 - x0 + 1, y1 - y0 + 1, x0, y0, max(advance_min, x1 + 1), 0), rle(a4)


def emit_font(name, g, data):
    cp, w, h, xo, yo, adv, off = g
    print(f"static const uint8_t {name}_data[{len(data)}] = {{")
    for i in range(0, len(data), 20):
        print("    " + ", ".join(f"0x{v:02X}" for v in data[i:i + 20]) + ",")
    print("};\n")
    print(f"static const lcd_glyph_t {name}_glyphs[1] = {{{{0x{cp:04X}, {w}, {h}, {xo}, {yo}, {adv}, {off}}}}};")
    print(f"static const lcd_font_t {name} = {{{name}_glyphs, {name}_data, 1, {yo + h}, {yo + h}}};\n")
    return len(data) + 10


def main():
    svg = open(sys.argv[1], encoding="utf-8").read()
    tile = int(sys.argv[2]) if len(sys.argv) > 2 else 64
    word_w = int(sys.argv[3]) if len(sys.argv) > 3 else tile

    paths = re.findall(r'<path d="([^"]+)"', svg)
    m = re.search(r'translate\(([\d.]+) ([\d.]+)\) scale\(([\d.]+)\)"><path fill="[^"]+" d="([^"]+)"', svg)
    tx, ty, ts, word_d = float(m.group(1)), float(m.group(2)), float(m.group(3)), m.group(4)

    # Bolt: same scale as the tile.
    scale = tile / 64.0
    bolt = [[(x * scale, y * scale) for x, y in p] for p in subpaths(paths[0])]

    # Wordmark: in SVG units, then fitted to word_w pixels from its own box.
    word = [[(tx + x * ts, ty + y * ts) for x, y in p] for p in subpaths(word_d)]
    xs = [x for p in word for x, _ in p]
    ys = [y for p in word for _, y in p]
    ws = word_w / (max(xs) - min(xs))
    word = [[((x - min(xs)) * ws, (y - min(ys)) * ws) for x, y in p] for p in word]
    word_h = math.ceil((max(ys) - min(ys)) * ws)

    tile_color = re.search(r'<rect[^>]*fill="#([0-9a-fA-F]{6})"', svg).group(1)
    r, g, b = (int(tile_color[i:i + 2], 16) for i in (0, 2, 4))

    g_bolt, d_bolt = glyph(raster(bolt, tile, tile), 0xE000, tile)
    g_word, d_word = glyph(raster(word, word_w, word_h), 0xE001, 0)

    print("// Generated by tools/gen_brand.py from tools/brand/fixfast-logo.svg. Do not edit.")
    print("#pragma once\n")
    print('#include "display.h"')
    print('#include "ui_fonts.h"\n')
    print(f"#define BRAND_TILE {tile}u")
    print(f"#define BRAND_TILE_R {round(tile * TILE_RADIUS)}u")
    print(f"#define BRAND_TILE_COLOR RGB565({r}, {g}, {b}) // #{tile_color.lower()}")
    print(f"#define BRAND_WORD_W {g_word[3] + g_word[1]}u")
    print(f"#define BRAND_WORD_H {g_word[4] + g_word[2]}u")
    print('#define BRAND_BOLT "\\xEE\\x80\\x80" // U+E000')
    print('#define BRAND_WORD "\\xEE\\x80\\x81" // U+E001\n')
    total = emit_font("font_brand_bolt", g_bolt, d_bolt)
    total += emit_font("font_brand_word", g_word, d_word)
    sys.stderr.write(f"brand: {total} bytes of flash, tile {tile}x{tile}, "
                     f"word {g_word[3] + g_word[1]}x{g_word[4] + g_word[2]} px\n")


if __name__ == "__main__":
    main()
