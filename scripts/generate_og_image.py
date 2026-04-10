"""Generate a 1200x630 Open Graph image for HostGuide social sharing.

Usage:
    python scripts/generate_og_image.py

Output:
    src/static/og-image.png
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── Paths ──
ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "static"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = OUTPUT_DIR / "og-image.png"

# ── Dimensions ──
WIDTH, HEIGHT = 1200, 630

# ── Colors ──
DARK_TEAL = (0, 77, 64)       # #004d40
MED_TEAL = (0, 137, 123)      # #00897b
ACCENT_TEAL = (77, 182, 172)  # #4DB6AC
WHITE = (255, 255, 255)
WHITE_80 = (255, 255, 255, 204)   # 80% opacity
WHITE_70 = (255, 255, 255, 179)   # 70% opacity
WHITE_50 = (255, 255, 255, 128)   # 50% opacity
CARD_BG = (255, 255, 255, 240)
CARD_SHADOW = (0, 0, 0, 40)
TEXT_DARK = (30, 30, 30)
TEXT_MED = (80, 80, 80)


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a font at the given size, falling back to default."""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Pillow < 10 does not support size param
        return ImageFont.load_default()


def draw_gradient(img: Image.Image) -> None:
    """Draw a horizontal gradient from DARK_TEAL to MED_TEAL."""
    draw = ImageDraw.Draw(img)
    for x in range(WIDTH):
        t = x / WIDTH
        r = int(DARK_TEAL[0] + (MED_TEAL[0] - DARK_TEAL[0]) * t)
        g = int(DARK_TEAL[1] + (MED_TEAL[1] - DARK_TEAL[1]) * t)
        b = int(DARK_TEAL[2] + (MED_TEAL[2] - DARK_TEAL[2]) * t)
        draw.line([(x, 0), (x, HEIGHT)], fill=(r, g, b))


def draw_left_text(overlay: Image.Image) -> None:
    """Draw the headline text on the left 60% of the image."""
    draw = ImageDraw.Draw(overlay)

    x_start = 70
    y = 140

    # "HostGuide" - large title
    font_title = load_font(54)
    draw.text((x_start, y), "HostGuide", fill=WHITE, font=font_title)
    y += 70

    # "Neighborhood Guides"
    font_sub = load_font(38)
    draw.text((x_start, y), "Neighborhood Guides", fill=WHITE, font=font_sub)
    y += 52

    # "for Airbnb Hosts"
    font_small = load_font(28)
    draw.text((x_start, y), "for Airbnb Hosts", fill=WHITE_80, font=font_small)
    y += 50

    # Divider line
    draw.line([(x_start, y), (x_start + 300, y)], fill=WHITE_50, width=2)
    y += 20

    # Tagline
    font_tag = load_font(20)
    draw.text(
        (x_start, y),
        "Paste your listing. Get a polished",
        fill=WHITE_70,
        font=font_tag,
    )
    y += 28
    draw.text(
        (x_start, y),
        "guide in 60 seconds.",
        fill=WHITE_70,
        font=font_tag,
    )


def draw_card_mockup(overlay: Image.Image) -> None:
    """Draw a rotated white card mockup on the right side."""
    card_w, card_h = 340, 320
    card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
    card_draw = ImageDraw.Draw(card)

    # Card background with rounded corners
    radius = 16
    card_draw.rounded_rectangle(
        [(0, 0), (card_w - 1, card_h - 1)],
        radius=radius,
        fill=CARD_BG,
        outline=(200, 200, 200, 180),
        width=1,
    )

    # Teal header bar
    card_draw.rounded_rectangle(
        [(0, 0), (card_w - 1, 50)],
        radius=radius,
        fill=(*MED_TEAL, 255),
    )
    # Fill the bottom corners of the header so it looks flat at the bottom
    card_draw.rectangle([(0, 30), (card_w - 1, 50)], fill=(*MED_TEAL, 255))

    font_header = load_font(16)
    card_draw.text((16, 16), "Neighborhood Guide", fill=WHITE, font=font_header)

    # Place entries
    places = [
        ("Cafe du Soleil", "3 min walk"),
        ("Migros", "4 min walk"),
        ("Jet d'Eau", "8 min"),
    ]

    font_place = load_font(16)
    font_dist = load_font(14)
    entry_y = 70

    for name, dist in places:
        # Small teal dot
        dot_cx, dot_cy = 24, entry_y + 12
        card_draw.ellipse(
            [(dot_cx - 5, dot_cy - 5), (dot_cx + 5, dot_cy + 5)],
            fill=(*ACCENT_TEAL, 255),
        )

        # Place name
        card_draw.text((42, entry_y), name, fill=TEXT_DARK, font=font_place)
        # Distance
        card_draw.text((42, entry_y + 24), dist, fill=TEXT_MED, font=font_dist)

        # Separator line
        sep_y = entry_y + 54
        card_draw.line(
            [(16, sep_y), (card_w - 16, sep_y)],
            fill=(220, 220, 220, 200),
            width=1,
        )
        entry_y += 68

    # Add a subtle bottom text
    font_tiny = load_font(12)
    card_draw.text(
        (16, card_h - 35),
        "Powered by HostGuide",
        fill=(*ACCENT_TEAL, 200),
        font=font_tiny,
    )

    # Rotate card slightly
    rotated = card.rotate(3, expand=True, resample=Image.BICUBIC)

    # Position on the right side of the image
    paste_x = 760
    paste_y = 120
    overlay.paste(rotated, (paste_x, paste_y), rotated)


def draw_bottom_url(overlay: Image.Image) -> None:
    """Draw the URL at the bottom center."""
    draw = ImageDraw.Draw(overlay)
    font_url = load_font(18)
    text = "host-guide.net"
    bbox = draw.textbbox((0, 0), text, font=font_url)
    text_w = bbox[2] - bbox[0]
    x = (WIDTH - text_w) // 2
    y = HEIGHT - 45
    draw.text((x, y), text, fill=WHITE_70, font=font_url)


def draw_decorative_elements(overlay: Image.Image) -> None:
    """Add subtle decorative touches."""
    draw = ImageDraw.Draw(overlay)

    # Top-right decorative circle (subtle)
    draw.ellipse(
        [(WIDTH - 180, -60), (WIDTH + 20, 140)],
        fill=(255, 255, 255, 12),
    )

    # Bottom-left decorative circle
    draw.ellipse(
        [(-80, HEIGHT - 160), (120, HEIGHT + 40)],
        fill=(255, 255, 255, 10),
    )

    # Thin top accent line
    draw.rectangle([(0, 0), (WIDTH, 4)], fill=(*ACCENT_TEAL, 200))


def main() -> None:
    """Generate the OG image."""
    # Base image with gradient (RGB)
    base = Image.new("RGB", (WIDTH, HEIGHT), DARK_TEAL)
    draw_gradient(base)

    # RGBA overlay for alpha-blended elements
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))

    draw_decorative_elements(overlay)
    draw_left_text(overlay)
    draw_card_mockup(overlay)
    draw_bottom_url(overlay)

    # Composite overlay onto base
    base_rgba = base.convert("RGBA")
    composite = Image.alpha_composite(base_rgba, overlay)
    final = composite.convert("RGB")

    # Save
    final.save(OUTPUT_PATH, "PNG", optimize=True)

    # Verify
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"OG image saved to: {OUTPUT_PATH}")
    print(f"Dimensions: {WIDTH}x{HEIGHT}")
    print(f"File size: {size_kb:.1f} KB")


if __name__ == "__main__":
    main()
