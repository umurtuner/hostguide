"""Generate all Product Hunt launch assets in one pass.

Output (static/ph/):
    logo_240.png              240x240 PNG (PH listing logo)
    gallery_1_hero.png        1270x760 landing page hero
    gallery_2_og.png          1270x760 branded title card (like OG, resized)
    gallery_3_guide.png       1270x760 sample guide page preview
    gallery_4_qr.png          1270x760 QR code section crop

Usage:
    python scripts/generate_ph_assets.py
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "ph"
OUT.mkdir(parents=True, exist_ok=True)

DARK_TEAL = (0, 77, 64)
MED_TEAL = (0, 137, 123)
ACCENT = (77, 182, 172)
WHITE = (255, 255, 255)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def generate_logo() -> Path:
    """240x240 PNG logo: teal map pin with white book icon."""
    size = 240
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Map pin teardrop (head circle + triangle tail)
    cx, cy = size // 2, 90
    r = 70
    draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=DARK_TEAL)
    tip_y = 215
    draw.polygon(
        [(cx - r * 0.55, cy + r * 0.6), (cx + r * 0.55, cy + r * 0.6), (cx, tip_y)],
        fill=DARK_TEAL,
    )

    # Inner white circle (book slot)
    inner_r = 34
    draw.ellipse(
        [(cx - inner_r, cy - inner_r), (cx + inner_r, cy + inner_r)],
        fill=WHITE,
    )
    # Tiny book: two stacked rounded rects
    book_w, book_h = 38, 26
    bx, by = cx - book_w // 2, cy - book_h // 2
    draw.rounded_rectangle(
        [(bx, by), (bx + book_w, by + book_h)],
        radius=3,
        fill=MED_TEAL,
    )
    draw.line(
        [(cx, by + 3), (cx, by + book_h - 3)],
        fill=WHITE, width=2,
    )

    path = OUT / "logo_240.png"
    img.save(path, "PNG", optimize=True)
    return path


def _gradient(img: Image.Image, w: int, h: int) -> None:
    d = ImageDraw.Draw(img)
    for x in range(w):
        t = x / w
        r = int(DARK_TEAL[0] + (MED_TEAL[0] - DARK_TEAL[0]) * t)
        g = int(DARK_TEAL[1] + (MED_TEAL[1] - DARK_TEAL[1]) * t)
        b = int(DARK_TEAL[2] + (MED_TEAL[2] - DARK_TEAL[2]) * t)
        d.line([(x, 0), (x, h)], fill=(r, g, b))


def generate_og_1270() -> Path:
    """1270x760 branded title card for PH gallery."""
    w, h = 1270, 760
    base = Image.new("RGB", (w, h), DARK_TEAL)
    _gradient(base, w, h)
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Top accent strip
    draw.rectangle([(0, 0), (w, 5)], fill=(*ACCENT, 220))
    # Decorative circles
    draw.ellipse([(w - 220, -80), (w + 40, 160)], fill=(255, 255, 255, 14))
    draw.ellipse([(-100, h - 200), (160, h + 60)], fill=(255, 255, 255, 12))

    x = 90
    y = 180
    draw.text((x, y), "HostGuide", fill=WHITE, font=_font(78))
    y += 100
    draw.text((x, y), "Personalized neighborhood guides", fill=WHITE, font=_font(46))
    y += 58
    draw.text((x, y), "for Airbnb hosts, in 60 seconds.", fill=WHITE, font=_font(46))
    y += 80
    draw.line([(x, y), (x + 380, y)], fill=(255, 255, 255, 130), width=3)
    y += 28
    draw.text(
        (x, y),
        "Paste your listing URL. Get a printable PDF.",
        fill=(255, 255, 255, 210),
        font=_font(26),
    )
    y += 38
    draw.text(
        (x, y),
        "Walking times, cafes, transit, groceries, local tips.",
        fill=(255, 255, 255, 180),
        font=_font(24),
    )

    # Bottom URL
    draw.text(
        (x, h - 60),
        "host-guide.net",
        fill=(255, 255, 255, 200),
        font=_font(22),
    )

    base_rgba = base.convert("RGBA")
    composite = Image.alpha_composite(base_rgba, overlay).convert("RGB")
    path = OUT / "gallery_2_og.png"
    composite.save(path, "PNG", optimize=True)
    return path


def _resize_pad(src: Path, out: Path, w: int = 1270, h: int = 760) -> None:
    """Fit an image into w×h keeping aspect, pad with dark teal."""
    img = Image.open(src).convert("RGB")
    img.thumbnail((w, h), Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), DARK_TEAL)
    px = (w - img.width) // 2
    py = (h - img.height) // 2
    canvas.paste(img, (px, py))
    canvas.save(out, "PNG", optimize=True)


def generate_hero_from_existing() -> Path:
    """Reuse static/landing_screenshot.png, re-fit to 1270×760."""
    src = ROOT / "static" / "landing_screenshot.png"
    out = OUT / "gallery_1_hero.png"
    if src.exists():
        _resize_pad(src, out)
    return out


def capture_live_pages() -> tuple[Path, Path]:
    """Capture the landing page + a sample guide via Playwright.

    Returns (guide_png, qr_crop_png). Falls back gracefully if Playwright missing.
    """
    guide_out = OUT / "gallery_3_guide.png"
    qr_out = OUT / "gallery_4_qr.png"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [warn] playwright missing; skipping guide + qr captures")
        return guide_out, qr_out

    sample_guide = next(
        (ROOT / "output" / "lisbon" / "guides").glob("*_guide.html"), None
    )
    if not sample_guide:
        sample_guide = next(
            (ROOT / "output" / "miami" / "guides").glob("*_guide.html"), None
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # Full guide page at 1270×760 (top-fold view)
        if sample_guide:
            page = browser.new_page(viewport={"width": 1270, "height": 760})
            page.goto(f"file://{sample_guide}")
            page.wait_for_timeout(1200)
            page.screenshot(path=str(guide_out), full_page=False)

            # QR-ish crop: take the bottom of a full-page screenshot
            fullpage = OUT / "_guide_full.png"
            page.screenshot(path=str(fullpage), full_page=True)
            img = Image.open(fullpage).convert("RGB")
            # Grab the last 760px of the guide (where QR typically sits)
            bottom = img.crop((0, max(0, img.height - 760), min(img.width, 1270), img.height))
            if bottom.width < 1270:
                canvas = Image.new("RGB", (1270, 760), DARK_TEAL)
                canvas.paste(bottom, ((1270 - bottom.width) // 2, 0))
                bottom = canvas
            else:
                bottom = bottom.resize((1270, 760), Image.LANCZOS)
            bottom.save(qr_out, "PNG", optimize=True)
            fullpage.unlink(missing_ok=True)

        browser.close()

    return guide_out, qr_out


def main() -> None:
    print("Generating Product Hunt assets → static/ph/")
    paths = []
    paths.append(generate_logo())
    paths.append(generate_hero_from_existing())
    paths.append(generate_og_1270())
    g, q = capture_live_pages()
    paths.extend([g, q])
    for p in paths:
        if p.exists():
            kb = p.stat().st_size / 1024
            print(f"  [ok] {p.relative_to(ROOT)}  {kb:.1f} KB")
        else:
            print(f"  [miss] {p.relative_to(ROOT)}  (missing — check manually)")


if __name__ == "__main__":
    main()
