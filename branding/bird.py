"""The DesOS bird, for the artwork generators (GRUB theme, installer).

`bird.svg` here is a copy of DEs-UI's master drawing (des-ui/branding/bird.svg);
tests/test_branding.py fails when the two differ. Its single path (moves, lines,
cubic curves, closes; filled even-odd) is read here, so no SVG renderer is needed.
"""
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
SVG = HERE / 'bird.svg'
VIEW = 1000.0
# The part of the 1000 x 1000 box the bird takes, so it can be placed by its ink.
INK = (68.0, 0.0, 932.0, 1000.0)
STEPS = 8


def aspect():
    """Width over height of the bird's ink."""
    return (INK[2] - INK[0]) / (INK[3] - INK[1])


def commands():
    d = re.search(r'\sd="([^"]+)"', SVG.read_text()).group(1)
    for op, args in re.findall(r'([MLCZ])([^MLCZ]*)', d):
        yield op, [float(v) for v in args.replace(',', ' ').split()]


def polygons():
    """The bird as closed polygons in box units, curves flattened."""
    out, current = [], []
    for op, v in commands():
        points = [(v[i], v[i + 1]) for i in range(0, len(v), 2)]
        if op == 'M':
            if len(current) > 2:
                out.append(current)
            current = list(points)
        elif op == 'L':
            current.extend(points)
        elif op == 'C':
            a = current[-1]
            for c1, c2, b in zip(points[0::3], points[1::3], points[2::3]):
                for step in range(1, STEPS + 1):
                    t = step / STEPS
                    u = 1 - t
                    w = (u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t)
                    current.append(tuple(w[0] * a[k] + w[1] * c1[k] + w[2] * c2[k] + w[3] * b[k] for k in (0, 1)))
                a = b
        elif op == 'Z' and len(current) > 2:
            out.append(current)
            current = []
    if len(current) > 2:
        out.append(current)
    return out


def mask(height, supersample=4):
    """The bird's ink as an 8-bit Pillow mask, `height` pixels tall, antialiased."""
    from PIL import Image, ImageChops, ImageDraw
    width = max(1, round(height * aspect()))
    big = (width * supersample, height * supersample)
    k = big[1] / (INK[3] - INK[1])
    result = Image.new('1', big, 0)
    for polygon in polygons():
        piece = Image.new('1', big, 0)
        ImageDraw.Draw(piece).polygon([((x - INK[0]) * k, (y - INK[1]) * k) for x, y in polygon], fill=1)
        # Even-odd: the eye, inside the head, cuts back out.
        result = ImageChops.logical_xor(result, piece)
    return result.convert('L').resize((width, height), Image.LANCZOS)


def trace(ctx, x, y, height):
    """Adds the bird to a cairo context's path, its ink `height` tall with its
    top-left corner at x, y. Fill with FILL_RULE_EVEN_ODD."""
    k = height / (INK[3] - INK[1])
    for op, v in commands():
        p = [(x + (v[i] - INK[0]) * k, y + (v[i + 1] - INK[1]) * k) for i in range(0, len(v), 2)]
        if op == 'M':
            ctx.move_to(*p[0])
        elif op == 'L':
            ctx.line_to(*p[0])
        elif op == 'C':
            ctx.curve_to(*p[0], *p[1], *p[2])
        else:
            ctx.close_path()
