#!/usr/bin/env python3
"""Regenerate the DesOS installer artwork from one description.

Every drawing is recorded once and written twice: as an SVG whose lettering is
converted to outlines (so it renders the same whether or not the DesOS fonts
are installed, and under QtSvg's limited text support), and as a PNG.

The PNGs are not a convenience. The live image ships no Qt SVG image-format
plugin, so QPixmap, QIcon and Qt style sheets can only decode PNG there. Only
Calamares's own ImageRegistry (QSvgRenderer) reads SVG. See ../DESIGN.md.

Needs python3-fonttools and python3-cairo on the build host; not at install
time. Run:  python3 installer/branding/build_art.py
"""
import math
import sys
from pathlib import Path

import cairo
import yaml
from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent
OUT = HERE / "desos"
FONTS = HERE.parent / "fonts"
sys.path.insert(0, str(HERE.parent.parent / "branding"))
import bird  # noqa: E402  the DesOS bird, read from branding/bird.svg

GROUND, PANEL, TRACK = "#06080b", "#0a0e12", "#141b22"
LINE, LINE_STRONG, EDGE = "#1a232b", "#2a3742", "#5b7385"
TEXT, SECONDARY, ACCENT = "#dce7ec", "#8fa3b0", "#4fd8ce"
DISABLED = "#7d909c"


class Outline(BasePen):
    """Collect one glyph as absolute cubic segments in drawing coordinates."""

    def __init__(self, glyphs, scale, dx, dy, into):
        super().__init__(glyphs)
        self.scale, self.dx, self.dy, self.into = scale, dx, dy, into

    def point(self, pt):
        return (self.dx + pt[0] * self.scale, self.dy - pt[1] * self.scale)

    def _moveTo(self, pt):
        self.into.append(("M", *self.point(pt)))

    def _lineTo(self, pt):
        self.into.append(("L", *self.point(pt)))

    def _curveToOne(self, a, b, c):
        self.into.append(("C", *self.point(a), *self.point(b), *self.point(c)))

    def _closePath(self):
        self.into.append(("Z",))

    def _endPath(self):
        self.into.append(("Z",))


class Face:
    def __init__(self, filename):
        self.font = TTFont(FONTS / filename)
        self.glyphs = self.font.getGlyphSet()
        self.cmap = self.font.getBestCmap()
        self.upm = self.font["head"].unitsPerEm

    def outline(self, text, x, y, size, tracking=0.0):
        """Outline text with its baseline starting at x, y. Returns (segments, width)."""
        scale = size / self.upm
        segments, pen_x = [], x
        for char in text:
            name = self.cmap[ord(char)]
            glyph = self.glyphs[name]
            glyph.draw(Outline(self.glyphs, scale, pen_x, y, segments))
            pen_x += glyph.width * scale + tracking
        return segments, pen_x - tracking - x


def rounded(x, y, w, h, r):
    k = r * 0.5523
    return [
        ("M", x + r, y), ("L", x + w - r, y), ("C", x + w - r + k, y, x + w, y + r - k, x + w, y + r),
        ("L", x + w, y + h - r), ("C", x + w, y + h - r + k, x + w - r + k, y + h, x + w - r, y + h),
        ("L", x + r, y + h), ("C", x + r - k, y + h, x, y + h - r + k, x, y + h - r),
        ("L", x, y + r), ("C", x, y + r - k, x + r - k, y, x + r, y), ("Z",),
    ]


def ellipse(cx, cy, rx, ry):
    kx, ky = rx * 0.5523, ry * 0.5523
    return [
        ("M", cx + rx, cy), ("C", cx + rx, cy + ky, cx + kx, cy + ry, cx, cy + ry),
        ("C", cx - kx, cy + ry, cx - rx, cy + ky, cx - rx, cy),
        ("C", cx - rx, cy - ky, cx - kx, cy - ry, cx, cy - ry),
        ("C", cx + kx, cy - ry, cx + rx, cy - ky, cx + rx, cy), ("Z",),
    ]


def polyline(*points, closed=False):
    segments = [("M", *points[0])] + [("L", *p) for p in points[1:]]
    return segments + [("Z",)] if closed else segments


class Drawing:
    def __init__(self, width, height, title):
        self.width, self.height, self.title, self.ops = width, height, title, []

    def fill(self, segments, colour):
        self.ops.append(("fill", segments, colour, 0))

    def fill_evenodd(self, segments, colour):
        """A fill where overlapping contours cut each other out (the bird's eye)."""
        self.ops.append(("fill-evenodd", segments, colour, 0))

    def stroke(self, segments, colour, width):
        self.ops.append(("stroke", segments, colour, width))

    def text(self, face, text, x, y, size, colour, tracking=0.0, anchor="start"):
        segments, width = face.outline(text, 0, y, size, tracking)
        shift = x - {"start": 0, "middle": width / 2, "end": width}[anchor]
        moved = [(s[0], *[v + shift if i % 2 == 0 else v for i, v in enumerate(s[1:])]) for s in segments]
        self.fill(moved, colour)
        return width

    def svg(self):
        def data(segments):
            return "".join(s[0] + " ".join(f"{v:.2f}".rstrip("0").rstrip(".") for v in s[1:]) for s in segments)

        body = []
        for kind, segments, colour, width in self.ops:
            if kind == "fill":
                body.append(f'  <path fill="{colour}" d="{data(segments)}"/>')
            elif kind == "fill-evenodd":
                body.append(f'  <path fill="{colour}" fill-rule="evenodd" d="{data(segments)}"/>')
            else:
                body.append(f'  <path fill="none" stroke="{colour}" stroke-width="{width:g}" d="{data(segments)}"/>')
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}">\n  <title>{self.title}</title>\n' + "\n".join(body) + "\n</svg>\n"
        )

    def png(self, path, scale=1):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, round(self.width * scale), round(self.height * scale))
        ctx = cairo.Context(surface)
        ctx.scale(scale, scale)
        for kind, segments, colour, width in self.ops:
            ctx.new_path()
            for s in segments:
                if s[0] == "M":
                    ctx.move_to(*s[1:])
                elif s[0] == "L":
                    ctx.line_to(*s[1:])
                elif s[0] == "C":
                    ctx.curve_to(*s[1:])
                else:
                    ctx.close_path()
            ctx.set_source_rgb(*[int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)])
            if kind in ("fill", "fill-evenodd"):
                ctx.set_fill_rule(cairo.FILL_RULE_EVEN_ODD if kind == "fill-evenodd" else cairo.FILL_RULE_WINDING)
                ctx.fill()
            else:
                ctx.set_line_width(width)
                ctx.stroke()
        surface.write_to_png(str(path))


def bird_outline(x, y, height):
    """The bird as drawing segments, its ink `height` tall, top-left at x, y."""
    k = height / (bird.INK[3] - bird.INK[1])
    segments = []
    for op, v in bird.commands():
        points = [(x + (v[i] - bird.INK[0]) * k, y + (v[i + 1] - bird.INK[1]) * k) for i in range(0, len(v), 2)]
        segments.append((op, *[c for p in points for c in p]))
    return segments


def welcome(strings, display, body, mono):
    d = Drawing(720, 220, f"{strings['versionedName']}: nothing is written until you confirm")
    d.fill(rounded(0.5, 0.5, 719, 219, 10), PANEL)
    d.stroke(rounded(0.5, 0.5, 719, 219, 10), LINE_STRONG, 1)
    # The bird beside the name, as in the DEs-UI lockup.
    d.fill_evenodd(bird_outline(54, 58, 66), TEXT)
    start = 54 + 66 * bird.aspect() + 12
    start += d.text(display, "Des", start, 112, 54, TEXT)
    d.text(display, "OS", start, 112, 54, ACCENT)
    d.text(mono, f"{strings['shortVersion']} PREVIEW  /  DEs-UI DESKTOP", 54, 142, 10.5, SECONDARY, tracking=1.2)
    d.text(body, "Built on Linux and Ubuntu 26.04", 54, 168, 13, SECONDARY)
    d.stroke(polyline((308.5, 30), (308.5, 190)), LINE_STRONG, 1)
    rows = (
        ("01", "Nothing is written yet", "You choose the disk and read the plan", "before anything on it changes."),
        ("02", "Works offline", "Everything installs from this USB.", "Nothing is sent anywhere."),
        ("03", "A few minutes", "Keep the power connected until", "the installer says All done."),
    )
    for index, (number, title, first, second) in enumerate(rows):
        top = 34 + index * 58
        d.text(mono, number, 336, top + 13, 11, ACCENT, tracking=1)
        d.text(display, title, 372, top + 14, 16, TEXT)
        d.text(body, first, 372, top + 32, 12.5, SECONDARY)
        d.text(body, second, 372, top + 47, 12.5, SECONDARY)
    return d


def icon():
    d = Drawing(128, 128, "Install DesOS")
    d.fill(rounded(7, 7, 114, 114, 20), PANEL)
    d.stroke(rounded(7, 7, 114, 114, 20), LINE_STRONG, 2)
    d.stroke(polyline((35, 82), (35, 97), (93, 97), (93, 82)), TEXT, 7)
    d.stroke(polyline((64, 30), (64, 74)), ACCENT, 7)
    d.stroke(polyline((47, 58), (64, 76), (81, 58)), ACCENT, 7)
    return d


def language():
    """Globe: recognisable to someone who cannot read the current language."""
    d = Drawing(48, 48, "Language")
    d.stroke(ellipse(24, 24, 17, 17), SECONDARY, 2.5)
    d.stroke(ellipse(24, 24, 7.5, 17), SECONDARY, 2.5)
    d.stroke(polyline((7, 24), (41, 24)), SECONDARY, 2.5)
    d.stroke(polyline((10.5, 15), (37.5, 15)), SECONDARY, 2)
    d.stroke(polyline((10.5, 33), (37.5, 33)), SECONDARY, 2)
    return d


def arrow(colour, up=False):
    d = Drawing(12, 12, "")
    points = ((2.5, 7.5), (6, 4), (9.5, 7.5)) if up else ((2.5, 4.5), (6, 8), (9.5, 4.5))
    d.stroke(polyline(*points), colour, 1.6)
    return d


def check(colour):
    d = Drawing(16, 16, "")
    d.stroke(polyline((3.5, 8.4), (6.7, 11.4), (12.5, 4.8)), colour, 2.2)
    return d


def dash(colour):
    d = Drawing(16, 16, "")
    d.stroke(polyline((4, 8), (12, 8)), colour, 2.2)
    return d


def main():
    strings = yaml.safe_load((OUT / "branding.desc").read_text())["strings"]
    display, body = Face("ChakraPetch-SemiBold.ttf"), Face("ChakraPetch-Medium.ttf")
    mono = Face("JetBrainsMono.ttf")
    (OUT / "img").mkdir(exist_ok=True)

    art = welcome(strings, display, body, mono)
    (OUT / "welcome.svg").write_text(art.svg())
    art.png(OUT / "welcome.png", 2)

    art = icon()
    (OUT / "icon.svg").write_text(art.svg())
    art.png(OUT / "icon.png", 2)

    (OUT / "language.svg").write_text(language().svg())

    controls = {
        "arrow-down": arrow(SECONDARY), "arrow-down-disabled": arrow(EDGE),
        "arrow-up": arrow(SECONDARY, up=True), "arrow-up-disabled": arrow(EDGE, up=True),
        "check": check(GROUND), "check-disabled": check(DISABLED),
        "dash": dash(GROUND),
    }
    for name, drawing in controls.items():
        drawing.png(OUT / "img" / f"{name}.png", 1)
        drawing.png(OUT / "img" / f"{name}@2x.png", 2)
    print(f"Wrote artwork into {OUT}")


if __name__ == "__main__":
    main()
