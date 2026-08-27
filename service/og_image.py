"""Renders the site-wide share-link image (og-image.png) with PIL rather
than a browser screenshot.

The old version (scripts/og-image-source.html) was rendered to PNG by
hand, once, with the "8,300+ open roles" / "1,000+ employers" stats
typed in as literal text -- every other number on this site is live,
and this one silently wasn't, going stale the moment the corpus moved
past it. Regenerating it here on every edge_export.py run, from the
same meta.json numbers the rest of the export already computed, fixes
that for good.

PIL over a headless-browser screenshot (the previous approach) or
satori/resvg (the JS ecosystem's usual answer) because neither belongs
in this container: no browser binary here, and satori+resvg pulls in a
WASM toolchain for what's fundamentally text on a gradient -- see
CONTRIBUTING.md's stated preference for hand-rolled over a dependency a
task doesn't need. Pillow plus one bundled font file (DejaVu Sans,
Bitstream Vera License -- see assets/fonts/LICENSE) covers it.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1200, 630

CANVAS = (13, 13, 12)
INK = (240, 237, 228)
INK_SECONDARY = (163, 157, 140)
INK_TERTIARY = (138, 132, 116)
ACCENT = (232, 149, 42)

FONTS_DIR = Path(__file__).parent / "assets" / "fonts"
BOLT_PATH = [(13, 2), (3, 14), (12, 14), (11, 22), (21, 10), (12, 10), (13, 2)]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(FONTS_DIR / name), size)


def _bolt(draw_size: int, color, alpha: int = 255) -> Image.Image:
    """Renders the brand bolt mark at an arbitrary size, from the same
    24x24 path used by the favicon/app icons, so every rendering of the
    mark across the site is geometrically the same shape."""
    layer = Image.new("RGBA", (draw_size, draw_size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    scale = draw_size / 24.0
    pts = [(x * scale, y * scale) for x, y in BOLT_PATH]
    d.polygon(pts, fill=(*color, alpha))
    return layer


def _radial_glow(size: tuple[int, int], center: tuple[int, int], radius: int, color, max_alpha: int) -> Image.Image:
    """A soft radial glow: concentric circles from the outside in, alpha
    rising as radius shrinks. Cheap approximation of a radial gradient
    without pulling in numpy for a per-pixel one."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    steps = 60
    cx, cy = center
    for i in range(steps, 0, -1):
        r = radius * i / steps
        alpha = int(max_alpha * (1 - i / steps) ** 2)
        if alpha <= 0:
            continue
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*color, alpha))
    return layer


def render_og_image(stats: dict) -> bytes:
    """`stats` needs total_open_postings and total_companies (already
    computed by build_metadata -- see edge_export.py)."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), (*CANVAS, 255))

    # Top-right glow, matching the site's own header accent glow.
    glow = _radial_glow((WIDTH, HEIGHT), (WIDTH - 60, -80), 520, ACCENT, max_alpha=40)
    img.alpha_composite(glow)

    # A large, very low-opacity bolt watermark on the right half -- the
    # previous image left that whole side empty, which is the actual
    # "sloppy" the front-page share card was showing: a wide 1200x630
    # canvas with real content packed into the left 70% and nothing
    # anchoring the rest. This fills it without adding new information
    # to read, just brand texture.
    watermark = _bolt(560, ACCENT, alpha=16)
    img.alpha_composite(watermark, (WIDTH - 560 + 90, HEIGHT - 560 + 40))

    d = ImageDraw.Draw(img)
    pad_x = 96

    # Wordmark
    small_bolt = _bolt(48, ACCENT)
    img.alpha_composite(small_bolt, (pad_x, 60))
    f_brand = _font(36, bold=True)
    f_brand_dim = _font(36, bold=False)
    brand_x = pad_x + 48 + 20
    d.text((brand_x, 68), "lilguy", font=f_brand, fill=INK)
    brand_w = d.textlength("lilguy", font=f_brand)
    d.text((brand_x + brand_w, 72), ".win", font=f_brand_dim, fill=INK_TERTIARY)

    # Headline
    f_h1 = _font(58, bold=True)
    headline_y = 190
    d.text((pad_x, headline_y), "Every open internship,", font=f_h1, fill=INK)
    line2_y = headline_y + 72
    d.text((pad_x, line2_y), "in ", font=f_h1, fill=INK)
    in_w = d.textlength("in ", font=f_h1)
    d.text((pad_x + in_w, line2_y), "one fast feed.", font=f_h1, fill=ACCENT)

    # Subtitle
    f_tag = _font(24)
    tag_y = line2_y + 90
    d.text((pad_x, tag_y), "Search thousands of verified internship roles across hundreds of", font=f_tag, fill=INK_SECONDARY)
    d.text((pad_x, tag_y + 36), "employers, filtered by cycle, workplace, function, and industry.", font=f_tag, fill=INK_SECONDARY)

    # Stats -- the part that used to be stale text
    open_roles = stats.get("total_open_postings") or 0
    companies = stats.get("total_companies") or 0
    stat_items = [
        (f"{(open_roles // 100) * 100:,}+", "OPEN ROLES"),
        (f"{(companies // 100) * 100:,}+", "EMPLOYERS"),
        ("Live", "EDGE-SYNCED"),
    ]
    f_stat_n = _font(29, bold=True)
    f_stat_l = _font(15)
    stats_y = tag_y + 110
    x = pad_x
    for value, label in stat_items:
        d.text((x, stats_y), value, font=f_stat_n, fill=ACCENT)
        d.text((x, stats_y + 40), label, font=f_stat_l, fill=INK_TERTIARY)
        x += max(d.textlength(value, font=f_stat_n), d.textlength(label, font=f_stat_l)) + 48

    from io import BytesIO
    buf = BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
