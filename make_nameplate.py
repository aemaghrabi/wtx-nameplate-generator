#!/usr/bin/env python3
"""
IoTech A2 rating plate (65 x 35 mm) -> laser-ready SVG generator.

Reproduces board "A2. Rating plate 65 x 35 mm, black anodized" (Page 2) from
the "Product Name Plates 70x35mm" design, fills its variable fields, and
writes two SVG files:

  <name>_positive.svg  engraving marks in black, no background
  <name>_negative.svg  solid black plate, marks knocked out in white

All text is converted to filled outlines (Inter, via HarfBuzz shaping).
The QR code and the Code 128 barcode are generated as vector paths.
Plate outline and mounting holes go in a separate "cut" layer as red hairlines.

Geometry is kept in design pixels (10 px = 1 mm); the SVG width/height are
set in millimetres so it imports at true size.

Usage:
  python make_nameplate.py --config plate.json
  python make_nameplate.py --serial WTX-000123 --barcode-data WTX-000123 \
      --qr-data "https://register.iotechglobal.io/p/WTX-000123"
  (CLI values override values from --config.)
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import barcode as pybarcode
import segno
import uharfbuzz as hb
from fontTools.misc.transform import Transform
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from PIL import Image

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
FONT_UPRIGHT = ASSETS / "fonts" / "Inter-Variable.ttf"
FONT_ITALIC = ASSETS / "fonts" / "Inter-Italic-Variable.ttf"
LOGO_PNG = ASSETS / "iotech-logo.png"

# Plate geometry in design px (10 px = 1 mm)
PX_PER_MM = 10.0
PLATE_W, PLATE_H, PLATE_R = 650.0, 350.0, 10.0
HOLE_CENTERS = [(45.0, 175.0), (605.0, 175.0)]

# Field defaults = the placeholders in the design
DEFAULTS: Dict[str, object] = {
    "product_name": "[Product name]",
    "product_type": "[Product type]",
    "model": "[MODEL NO.]",
    "serial": "[SERIAL NO.]",
    "supply": "[V DC]",
    "protection": "[IP RATING]",
    "temp": "[MIN … MAX °C]",
    "mfg_date": "[MM/YYYY]",
    "qr_data": None,          # None -> dashed placeholder box (not laser-ready)
    "qr_error": "m",
    "barcode_data": None,     # None -> design's dummy bars (not scannable)
    "barcode_text": None,     # None -> same as barcode_data
    "footer_1": "IoTech LLC",
    "footer_2": "Dokki, Giza, Egypt",
    "footer_3": "iotechglobal.io",
    "ce_mark": True,
    "ex_mark": True,
    "hole_diameter_mm": 3.5,
    "out_dir": None,
    "output_name": None,
    "strict": False,
}

# Row labels are fixed by the design (not overridable)
ROWS = [
    ("MODEL", "model"), ("S/N", "serial"),
    ("SUPPLY", "supply"), ("PROTECTION", "protection"),
    ("TEMP.", "temp"), ("MFG. DATE", "mfg_date"),
]

# Dummy barcode bars from the design (viewBox 0 0 167 1): (x, width)
DUMMY_BARS = [
    (0, 2), (3, 1), (6, 1), (11, 3), (17, 2), (20, 1), (22, 2), (25, 3), (29, 1),
    (33, 1), (37, 2), (40, 1), (44, 2), (49, 1), (51, 3), (55, 2), (60, 1), (64, 1),
    (66, 1), (68, 1), (72, 2), (77, 1), (81, 2), (84, 3), (88, 2), (91, 2), (95, 2),
    (99, 1), (101, 3), (107, 2), (110, 1), (114, 3), (118, 2), (121, 1), (124, 2),
    (128, 3), (132, 2), (136, 1), (141, 1), (143, 1), (145, 2), (151, 1), (154, 2),
    (159, 3), (163, 1), (165, 2),
]


def f(v: float) -> str:
    """Compact number formatting for SVG."""
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# --------------------------------------------------------------------------
# Text to outlines
# --------------------------------------------------------------------------
class FontCache:
    def __init__(self) -> None:
        self._faces: Dict[str, hb.Face] = {}
        self._fonts: Dict[Tuple[str, int, float], hb.Font] = {}

    def font(self, path: Path, weight: int, size: float) -> hb.Font:
        key = (str(path), weight, size)
        if key not in self._fonts:
            if str(path) not in self._faces:
                if not path.exists():
                    sys.exit(f"Font not found: {path}")
                self._faces[str(path)] = hb.Face(hb.Blob.from_file_path(str(path)))
            font = hb.Font(self._faces[str(path)])
            # Browser default font-optical-sizing:auto maps opsz to px size
            font.set_variations({"wght": weight, "opsz": max(14.0, min(32.0, size))})
            self._fonts[key] = font
        return self._fonts[key]


FONTS = FontCache()


class Text:
    """Shaped run of text, measurable and drawable as an SVG path."""

    def __init__(self, text: str, size: float, weight: int = 400,
                 letter_spacing_em: float = 0.0, italic: bool = False) -> None:
        self.text = text
        self.size = size
        self.font = FONTS.font(FONT_ITALIC if italic else FONT_UPRIGHT, weight, size)
        self.upem = self.font.face.upem
        self.scale = size / self.upem
        self.ls = letter_spacing_em * size
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        feats = {"kern": True, "liga": self.ls == 0, "calt": self.ls == 0}
        hb.shape(self.font, buf, feats)
        self.glyphs = list(zip(buf.glyph_infos, buf.glyph_positions))
        # CSS letter-spacing is added after every character, including the last
        self.width = sum(p.x_advance for _, p in self.glyphs) * self.scale + self.ls * len(self.glyphs)

    def path_d(self, x: float, baseline: float) -> str:
        pen = SVGPathPen(None, ntos=f)
        cx = x
        for info, pos in self.glyphs:
            t = Transform(self.scale, 0, 0, -self.scale,
                          cx + pos.x_offset * self.scale,
                          baseline - pos.y_offset * self.scale)
            self.font.draw_glyph_with_pen(info.codepoint, TransformPen(pen, t))
            cx += pos.x_advance * self.scale + self.ls
        return pen.getCommands()

    def place(self, anchor: str, x: float, baseline: float) -> Tuple[str, float, float]:
        """anchor: 'start' | 'middle' | 'end'. Returns (d, x0, x1)."""
        if anchor == "end":
            x0 = x - self.width
        elif anchor == "middle":
            x0 = x - self.width / 2
        else:
            x0 = x
        return self.path_d(x0, baseline), x0, x0 + self.width


# Inter vertical metrics (hhea: ascender 1984, descender -494, upem 2048)
ASC = 1984 / 2048
DESC = 494 / 2048
NORMAL_LH = ASC + DESC  # CSS line-height: normal


def baseline_in_box(top: float, size: float, line_height: Optional[float] = None) -> float:
    lh = line_height if line_height is not None else NORMAL_LH * size
    return top + (lh - NORMAL_LH * size) / 2 + ASC * size


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------
def rect_d(x: float, y: float, w: float, h: float) -> str:
    return f"M{f(x)} {f(y)}h{f(w)}v{f(h)}h{f(-w)}Z"


def rounded_rect_d(x: float, y: float, w: float, h: float, r: float) -> str:
    return (f"M{f(x + r)} {f(y)}H{f(x + w - r)}A{f(r)} {f(r)} 0 0 1 {f(x + w)} {f(y + r)}"
            f"V{f(y + h - r)}A{f(r)} {f(r)} 0 0 1 {f(x + w - r)} {f(y + h)}"
            f"H{f(x + r)}A{f(r)} {f(r)} 0 0 1 {f(x)} {f(y + h - r)}"
            f"V{f(y + r)}A{f(r)} {f(r)} 0 0 1 {f(x + r)} {f(y)}Z")


def offset_polygon(pts: List[Tuple[float, float]], d: float) -> List[Tuple[float, float]]:
    """Offset a convex CW/CCW polygon by d (positive = outward), mitered joins."""
    n = len(pts)
    gx = sum(p[0] for p in pts) / n
    gy = sum(p[1] for p in pts) / n
    lines = []
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        ln = math.hypot(dx, dy)
        nx, ny = dy / ln, -dx / ln
        if nx * ((x1 + x2) / 2 - gx) + ny * ((y1 + y2) / 2 - gy) < 0:
            nx, ny = -nx, -ny  # make the normal point outward
        lines.append(((x1 + nx * d, y1 + ny * d), (dx, dy)))
    out = []
    for i in range(n):
        (p1, v1), (p2, v2) = lines[i - 1], lines[i]
        den = v1[0] * v2[1] - v1[1] * v2[0]
        t = ((p2[0] - p1[0]) * v2[1] - (p2[1] - p1[1]) * v2[0]) / den
        out.append((p1[0] + v1[0] * t, p1[1] + v1[1] * t))
    return out


def poly_d(pts: List[Tuple[float, float]]) -> str:
    return "M" + "L".join(f"{f(x)} {f(y)}" for x, y in pts) + "Z"


def ce_mark_d(x: float, y: float) -> str:
    """CE mark, design svg 64x44 with viewBox '16 0 82 56', strokes 6 -> filled."""
    s = min(64 / 82, 44 / 56)
    ox = x + (64 - 82 * s) / 2 - 16 * s
    oy = y + (44 - 56 * s) / 2
    T = lambda px, py: (ox + px * s, oy + py * s)  # noqa: E731
    parts = []
    for cx in (44.0, 94.0):  # left half-ring, centre (cx, 28), r 22, stroke 6
        ro, ri = 25 * s, 19 * s
        a = T(cx, 3); b = T(cx, 53); c = T(cx, 47); d_ = T(cx, 9)
        parts.append(f"M{f(a[0])} {f(a[1])}A{f(ro)} {f(ro)} 0 0 0 {f(b[0])} {f(b[1])}"
                     f"L{f(c[0])} {f(c[1])}A{f(ri)} {f(ri)} 0 0 1 {f(d_[0])} {f(d_[1])}Z")
    p0 = T(74, 25)
    parts.append(rect_d(p0[0], p0[1], 16 * s, 6 * s))
    return "".join(parts)


def ex_mark_d(x: float, y: float) -> str:
    """Ex hexagon (stroke 2.5 -> filled ring) plus italic 'εx' outlines. Box 46x40."""
    hexa = [(11.5, 1.5), (34.5, 1.5), (44.5, 20), (34.5, 38.5), (11.5, 38.5), (1.5, 20)]
    hexa = [(x + px, y + py) for px, py in hexa]
    outer = offset_polygon(hexa, 1.25)
    inner = offset_polygon(hexa, -1.25)
    t = Text("εx", 17, 600, italic=True)
    d_text, _, _ = t.place("middle", x + 23, y + 26.5)
    return poly_d(outer) + poly_d(list(reversed(inner))) + d_text


def qr_d(data: str, x: float, y: float, size: float, error: str) -> Tuple[str, int, int]:
    qr = segno.make(data, error=error, micro=False, boost_error=False)
    rows = [list(r) for r in qr.matrix_iter(border=0)]
    n = len(rows)
    m = size / n
    parts = []
    for j, row in enumerate(rows):
        i = 0
        while i < n:
            if row[i]:
                k = i
                while k < n and row[k]:
                    k += 1
                parts.append(rect_d(x + i * m, y + j * m, (k - i) * m, m))
                i = k
            else:
                i += 1
    return "".join(parts), n, qr.version


def code128_d(data: str, x: float, y: float, w: float, h: float) -> Tuple[str, int]:
    bars = pybarcode.Code128(data).build()[0]  # '1' = bar, '0' = space, no quiet zone
    n = len(bars)
    m = w / n
    parts = []
    for mo in re.finditer(r"1+", bars):
        parts.append(rect_d(x + mo.start() * m, y, (mo.end() - mo.start()) * m, h))
    return "".join(parts), n


def dummy_bars_d(x: float, y: float, w: float, h: float) -> str:
    m = w / 167
    return "".join(rect_d(x + bx * m, y, bw * m, h) for bx, bw in DUMMY_BARS)


def logo_data_uri(color: Tuple[int, int, int]) -> str:
    """Single-tone recolor of the raster logo, keeping its alpha (as the design's CSS mask does)."""
    im = Image.open(LOGO_PNG).convert("RGBA")
    solid = Image.new("RGBA", im.size, color + (255,))
    solid.putalpha(im.getchannel("A"))
    buf = io.BytesIO()
    solid.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# --------------------------------------------------------------------------
# Plate builder
# --------------------------------------------------------------------------
class Overflow(list):
    def check(self, what: str, x0: float, x1: float, lo: float, hi: float) -> None:
        if x0 < lo - 0.01 or x1 > hi + 0.01:
            self.append(f"{what}: spans {x0 / 10:.2f}..{x1 / 10:.2f} mm, "
                        f"allowed {lo / 10:.2f}..{hi / 10:.2f} mm")


def build_engraving(v: Dict[str, object], warnings: List[str], overflow: Overflow) -> Tuple[str, Dict[str, str]]:
    """Return (single path d for all vector marks, logo placement attrs)."""
    d: List[str] = []

    # Header: logo box 166x60 at (26, 23); title block right-aligned at x=520,
    # vertically centred in the 62 px header band (top 22).
    header_center = 22 + 62 / 2
    name = Text(str(v["product_name"]), 26, 500, -0.01)
    ptype = Text(str(v["product_type"]).upper(), 14, 400, 0.05)
    name_lh = 26 * 1.1
    type_lh = NORMAL_LH * 14
    block_h = name_lh + 4 + type_lh
    top = header_center - block_h / 2
    s, x0, x1 = name.place("end", 520, baseline_in_box(top, 26, name_lh))
    d.append(s); overflow.check("product_name", x0, x1, 26 + 166 + 16, 520)
    s, x0, x1 = ptype.place("end", 520, baseline_in_box(top + name_lh + 4, 14))
    d.append(s); overflow.check("product_type", x0, x1, 26 + 166 + 16, 520)

    # Divider 2 px
    d.append(rect_d(26, 96, 520 - 26, 2))

    # QR area 90x90 at (534, 22)
    if v.get("qr_data"):
        qd, n, ver = qr_d(str(v["qr_data"]), 534, 22, 90, str(v["qr_error"]))
        mod_mm = 9.0 / n
        if mod_mm < 0.25:
            warnings.append(f"QR is version {ver} ({n}x{n}); module {mod_mm:.3f} mm is very small "
                            f"for laser marking. Shorten --qr-data or lower --qr-error.")
        d.append(qd)
    else:
        warnings.append("No qr_data given: drawing the dashed QR placeholder (not for production).")
        d.append(None)  # placeholder drawn separately as stroked preview

    # Data grid: x 70..580, 2 cols of 241 px, col gap 28, row gap 10
    col_w = (580 - 70 - 28) / 2
    row_h = NORMAL_LH * 16 + 6 + 1
    for idx, (label, key) in enumerate(ROWS):
        r, c = divmod(idx, 2)
        cx = 70 + c * (col_w + 28)
        ty = 122 + r * (row_h + 10)
        lt = Text(label, 14, 400, 0.05)
        vt = Text(str(v[key]), 16, 500)
        sl, lx0, lx1 = lt.place("start", cx, baseline_in_box(ty, 14))
        sv, vx0, vx1 = vt.place("end", cx + col_w, baseline_in_box(ty, 16))
        d += [sl, sv]
        if vx0 < lx1 + 6:
            overflow.append(f"{key}: value '{v[key]}' collides with label '{label}' "
                            f"(needs {(lx1 + 6 - vx0) / 10:.2f} mm more)")
        d.append(rect_d(cx, ty + row_h - 1, col_w, 1))

    # Barcode 300x40 centred, HRI text below (block bottom at y = 302)
    bc_w, bc_h = 300.0, 40.0
    hri_lh = NORMAL_LH * 14
    bc_top = 350 - 48 - (bc_h + 3 + hri_lh)
    bc_x = (PLATE_W - bc_w) / 2
    if v.get("barcode_data"):
        bd, nmod = code128_d(str(v["barcode_data"]), bc_x, bc_top, bc_w, bc_h)
        mod_mm = bc_w / nmod / PX_PER_MM
        if mod_mm < 0.15:
            warnings.append(f"Code 128 module width {mod_mm:.3f} mm ({nmod} modules in 30 mm) is "
                            f"below 0.15 mm; scanning may be unreliable. Shorten --barcode-data.")
        d.append(bd)
        hri = v.get("barcode_text") or v["barcode_data"]
    else:
        warnings.append("No barcode_data given: drawing the design's dummy bars (not scannable).")
        d.append(dummy_bars_d(bc_x, bc_top, bc_w, bc_h))
        hri = v.get("barcode_text") or "[SERIAL NO.]"
    ht = Text(str(hri), 14, 500, 0.08)
    s, x0, x1 = ht.place("middle", PLATE_W / 2, baseline_in_box(bc_top + bc_h + 3, 14))
    d.append(s); overflow.check("barcode_text", x0, x1, 70, 580)

    # Marks, right-aligned at x=624, band y 280..324
    marks_left = 624.0
    x = 624.0
    if v["ex_mark"]:
        x -= 46
        d.append(ex_mark_d(x, 280 + 2))
        marks_left = x
        x -= 8
    if v["ce_mark"]:
        x -= 64
        d.append(ce_mark_d(x, 280))
        marks_left = x

    # Footer: 3 segments joined by middle dots, centred at x=325, bottom 26
    segs = [str(v[k]).strip() for k in ("footer_1", "footer_2", "footer_3") if str(v[k] or "").strip()]
    footer = " · ".join(segs)
    if footer:
        ft = Text(footer, 13, 400, 0.02)
        s, x0, x1 = ft.place("middle", PLATE_W / 2, baseline_in_box(350 - 26 - NORMAL_LH * 13, 13))
        d.append(s)
        right_lim = min(570.0, marks_left - 8) if (v["ce_mark"] or v["ex_mark"]) else 570.0
        overflow.check("footer", x0, x1, 80, right_lim)

    logo = {"x": "26", "y": "23", "width": "166", "height": "60"}
    return d, logo


def qr_placeholder_svg(color: str) -> str:
    return (f'<g id="qr-placeholder" fill="none" stroke="{color}" stroke-width="2">'
            f'<rect x="535" y="23" width="88" height="88" rx="3" stroke-dasharray="6 4"/>'
            f'<text x="579" y="72" text-anchor="middle" font-family="Inter, sans-serif" '
            f'font-size="13" font-weight="500" fill="{color}" stroke="none">QR</text></g>')


def render_svg(v: Dict[str, object], polarity: str, parts: List[Optional[str]],
               logo: Dict[str, str]) -> str:
    if polarity == "positive":
        mark, bg, logo_rgb = "#000000", None, (0, 0, 0)
    else:
        mark, bg, logo_rgb = "#ffffff", "#000000", (255, 255, 255)
    w_mm, h_mm = PLATE_W / PX_PER_MM, PLATE_H / PX_PER_MM
    hole_r = float(v["hole_diameter_mm"]) * PX_PER_MM / 2
    vec = "".join(p for p in parts if p)
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        f'width="{f(w_mm)}mm" height="{f(h_mm)}mm" viewBox="0 0 {f(PLATE_W)} {f(PLATE_H)}">',
        f'<title>IoTech A2 rating plate {f(w_mm)} x {f(h_mm)} mm, {polarity}</title>',
        f'<desc>Scale 10 units = 1 mm. Serial: {esc(str(v["serial"]))}</desc>',
        '<g id="engrave" inkscape:groupmode="layer" inkscape:label="Engrave">',
    ]
    if bg:
        out.append(f'<path id="plate-fill" fill="{bg}" d="{rounded_rect_d(0, 0, PLATE_W, PLATE_H, PLATE_R)}"/>')
    out.append(f'<image id="logo" x="{logo["x"]}" y="{logo["y"]}" width="{logo["width"]}" '
               f'height="{logo["height"]}" preserveAspectRatio="xMidYMid meet" '
               f'xlink:href="{logo_data_uri(logo_rgb)}" href="{logo_data_uri(logo_rgb)}"/>')
    out.append(f'<path id="marks" fill="{mark}" fill-rule="nonzero" d="{vec}"/>')
    if None in parts:
        out.append(qr_placeholder_svg(mark))
    out.append("</g>")
    out.append('<g id="cut" inkscape:groupmode="layer" inkscape:label="Cut" '
               'fill="none" stroke="#ff0000" stroke-width="0.1">')
    out.append(f'<path id="outline" d="{rounded_rect_d(0, 0, PLATE_W, PLATE_H, PLATE_R)}"/>')
    for i, (cx, cy) in enumerate(HOLE_CENTERS, 1):
        out.append(f'<circle id="hole-{i}" cx="{f(cx)}" cy="{f(cy)}" r="{f(hole_r)}"/>')
    out.append("</g>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> Dict[str, object]:
    p = argparse.ArgumentParser(
        description="Generate the IoTech A2 rating plate (65 x 35 mm) as positive and negative SVGs.")
    p.add_argument("--config", type=Path, help="JSON file with field values (CLI overrides it)")
    g = p.add_argument_group("plate text")
    g.add_argument("--product-name", dest="product_name")
    g.add_argument("--product-type", dest="product_type")
    g.add_argument("--model")
    g.add_argument("--serial")
    g.add_argument("--supply")
    g.add_argument("--protection")
    g.add_argument("--temp")
    g.add_argument("--mfg-date", dest="mfg_date")
    g.add_argument("--footer-1", dest="footer_1", help="footer segment 1 (default: IoTech LLC)")
    g.add_argument("--footer-2", dest="footer_2", help="footer segment 2 (default: Dokki, Giza, Egypt)")
    g.add_argument("--footer-3", dest="footer_3", help="footer segment 3 (default: iotechglobal.io)")
    c = p.add_argument_group("codes")
    c.add_argument("--qr-data", dest="qr_data", help="text/URL encoded in the 9 x 9 mm QR code")
    c.add_argument("--qr-error", dest="qr_error", choices=["l", "m", "q", "h"], help="QR error correction (default m)")
    c.add_argument("--barcode-data", dest="barcode_data", help="text encoded in the Code 128 barcode")
    c.add_argument("--barcode-text", dest="barcode_text",
                   help="human-readable line under the barcode (default: barcode data)")
    m = p.add_argument_group("marks")
    m.add_argument("--ce", dest="ce_mark", action=argparse.BooleanOptionalAction, default=None,
                   help="show the CE mark (default on)")
    m.add_argument("--ex", dest="ex_mark", action=argparse.BooleanOptionalAction, default=None,
                   help="show the Ex mark (default on)")
    o = p.add_argument_group("output")
    o.add_argument("--hole-diameter-mm", dest="hole_diameter_mm", type=float, help="mounting hole diameter (default 3.5)")
    o.add_argument("--out-dir", dest="out_dir", type=Path, help="output folder (default: this script's folder)")
    o.add_argument("--output-name", dest="output_name", help="file base name (default: nameplate-A2_<serial>)")
    o.add_argument("--strict", dest="strict", action="store_true", default=None,
                   help="fail instead of warning when text overflows its area")
    a = p.parse_args(argv)

    values: Dict[str, object] = dict(DEFAULTS)
    if a.config:
        cfg = json.loads(a.config.read_text(encoding="utf-8"))
        unknown = set(cfg) - set(DEFAULTS)
        if unknown:
            p.error(f"unknown keys in {a.config}: {', '.join(sorted(unknown))}")
        values.update(cfg)
    for k, val in vars(a).items():
        if k != "config" and val is not None:
            values[k] = val
    return values


def main(argv: Optional[List[str]] = None) -> int:
    v = parse_args(argv)
    warnings: List[str] = []
    overflow = Overflow()
    parts, logo = build_engraving(v, warnings, overflow)

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    for o in overflow:
        print(f"overflow: {o}", file=sys.stderr)
    if overflow and v["strict"]:
        print("error: text overflow with --strict; nothing written.", file=sys.stderr)
        return 2

    out_dir = Path(v["out_dir"]) if v["out_dir"] else HERE
    out_dir.mkdir(parents=True, exist_ok=True)
    base = v["output_name"] or f"nameplate-A2_{v['serial']}"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", str(base)).strip("_") or "nameplate-A2"
    for pol in ("positive", "negative"):
        path = out_dir / f"{base}_{pol}.svg"
        path.write_text(render_svg(v, pol, parts, logo), encoding="utf-8")
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
