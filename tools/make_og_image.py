#!/usr/bin/env python3
"""Generate an Open Graph (OG) social card image for each edition.

Renders a 1200×630 PNG with the edition title and the first sentence of
the Big Picture summary. Designed to be called from generate_news.py or
standalone from the command line.

Usage:
    python3 tools/make_og_image.py \\
        --title "AI News Digest — Morning Edition" \\
        --excerpt "OpenAI released GPT-5..." \\
        --date 2026-06-18 \\
        --edition Morning \\
        --output assets/og/2026-06-18-Morning.png

Requires: Pillow (pip install Pillow)
"""

import argparse
import logging
import re
import textwrap
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CARD_WIDTH = 1200
CARD_HEIGHT = 630
BG_COLOR = "#1a1a2e"
ACCENT_COLOR = "#e94560"
TITLE_COLOR = "#ffffff"
EXCERPT_COLOR = "#c4c4c4"
DATE_COLOR = "#888888"

# Attempt to load a good font; fall back to the default bitmap font.
def _load_font(size: int, bold: bool = False) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    """Load a TrueType font at the given size, falling back gracefully."""
    if Image is None:
        return None  # type: ignore[return-value]

    # Try common system font paths (Linux / macOS)
    candidates = []
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",  # macOS
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc",  # macOS
        ]

    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

    # Fall back to Pillow's default (bitmap) font
    try:
        return ImageFont.load_default(size)
    except TypeError:
        # Older Pillow versions don't support size parameter
        return ImageFont.load_default()


def first_sentence(text: str, max_chars: int = 200) -> str:
    """Extract the first sentence from text, respecting abbreviations.

    Falls back to truncation at max_chars if no sentence boundary is found.
    """
    if not text:
        return ""
    # Strip HTML tags
    clean = re.sub(r"<[^>]+>", "", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    # Find the first sentence boundary (. ! ?) that isn't mid-abbreviation
    # e.g. "U.S." or "Dr." should not be treated as sentence ends.
    match = re.search(r"(?<=[.!?])\s+(?=[A-Z])", clean)
    if match and match.start() <= max_chars:
        return clean[: match.start()].strip()
    # No sentence boundary found within limit — hard-truncate
    if len(clean) > max_chars:
        truncated = clean[:max_chars]
        # Try to break at a word boundary
        last_space = truncated.rfind(" ")
        if last_space > max_chars * 0.5:
            truncated = truncated[:last_space]
        return truncated.rstrip() + "…"
    return clean


def render_og_image(
    title: str,
    excerpt: str,
    date_str: str = "",
    edition_label: str = "",
    output_path: str | Path | None = None,
) -> "Image.Image | None":
    """Render a 1200×630 OG card PNG.

    Args:
        title: Edition title (e.g. "AI News Digest — Morning Edition").
        excerpt: First sentence of the Big Picture summary.
        date_str: Date string (e.g. "2026-06-18").
        edition_label: Edition name (e.g. "Morning").
        output_path: If provided, save the PNG to this path.

    Returns:
        The PIL Image object, or None if Pillow is not installed.
    """
    if Image is None:
        logger.error("Pillow is not installed; cannot generate OG image")
        return None

    img = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Draw accent stripe (left border)
    draw.rectangle([(0, 0), (8, CARD_HEIGHT)], fill=ACCENT_COLOR)

    # Title font (large)
    title_font = _load_font(48, bold=True)
    # Excerpt font (medium)
    excerpt_font = _load_font(28, bold=False)
    # Date/edition font (small)
    date_font = _load_font(22, bold=False)

    # Layout constants
    left_margin = 40
    right_margin = CARD_WIDTH - 40
    max_text_width = right_margin - left_margin

    # Draw date/edition line at top
    date_line = date_str
    if edition_label:
        date_line = f"{date_str} · {edition_label}" if date_str else edition_label
    if date_line:
        draw.text((left_margin, 50), date_line, fill=DATE_COLOR, font=date_font)

    # Draw title
    y = 100
    wrapped_title = textwrap.fill(title, width=40)
    for line in wrapped_title.split("\n"):
        draw.text((left_margin, y), line, fill=TITLE_COLOR, font=title_font)
        bbox = draw.textbbox((left_margin, y), line, font=title_font)
        y = bbox[3] + 12

    # Draw accent line under title
    y += 8
    draw.rectangle([(left_margin, y), (left_margin + 200, y + 4)], fill=ACCENT_COLOR)
    y += 24

    # Draw excerpt (wrapped)
    wrapped_excerpt = textwrap.fill(excerpt, width=60) if excerpt else ""
    for line in wrapped_excerpt.split("\n"):
        if y > CARD_HEIGHT - 60:
            # Truncate if we'd overflow
            draw.text((left_margin, y), line[:80] + "…", fill=EXCERPT_COLOR, font=excerpt_font)
            break
        draw.text((left_margin, y), line, fill=EXCERPT_COLOR, font=excerpt_font)
        bbox = draw.textbbox((left_margin, y), line, font=excerpt_font)
        y = bbox[3] + 8

    # Draw site branding at bottom-right
    brand_font = _load_font(18, bold=False)
    draw.text(
        (right_margin - 160, CARD_HEIGHT - 40),
        "AI News Digest",
        fill=DATE_COLOR,
        font=brand_font,
    )

    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, "PNG", optimize=True)
        logger.info(f"OG image saved to {path} ({path.stat().st_size // 1024}KB)")

    return img


def generate_og_image_for_edition(
    edition: str,
    site_root: str | Path,
    global_summary_text: str | None,
) -> str | None:
    """Generate an OG image for an edition and return the relative path.

    This is the main entry point called from generate_news.py.

    Args:
        edition: Edition string like "2026-06-18-Morning" or "2026-06-18-morning".
        site_root: Path to the AI News site root.
        global_summary_text: The raw Big Picture summary text. May be None.

    Returns:
        Relative path to the generated PNG (e.g. "assets/og/2026-06-18-Morning.png"),
        or None if generation failed (graceful degradation).
    """
    if Image is None:
        logger.warning("Pillow not installed; OG image generation skipped")
        return None

    site_root = Path(site_root)

    # Parse edition components
    # edition format: "2026-06-18-Morning" or "2026-06-18-morning"
    parts = edition.rsplit("-", 1)
    if len(parts) == 2:
        date_str, edition_label = parts
        edition_label = edition_label.capitalize()
    else:
        date_str = edition
        edition_label = ""

    title = f"AI News Digest — {edition_label} Edition" if edition_label else "AI News Digest"
    excerpt = first_sentence(global_summary_text or "", max_chars=200)

    output_dir = site_root / "assets" / "og"
    filename = f"{date_str}-{edition_label}.png"
    output_path = output_dir / filename
    rel_path = f"assets/og/{filename}"

    try:
        result = render_og_image(
            title=title,
            excerpt=excerpt,
            date_str=date_str,
            edition_label=edition_label,
            output_path=output_path,
        )
        if result is not None:
            return rel_path
    except Exception as e:
        logger.error(f"OG image generation failed for {edition}: {e}")

    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Command-line entry point for standalone OG image generation."""
    parser = argparse.ArgumentParser(
        description="Generate an OG social card image for an AI News edition."
    )
    parser.add_argument("--title", required=True, help="Edition title")
    parser.add_argument("--excerpt", required=True, help="Big Picture first sentence")
    parser.add_argument("--date", default="", help="Date string (e.g. 2026-06-18)")
    parser.add_argument("--edition", default="", help="Edition label (Morning/Afternoon/Evening)")
    parser.add_argument("--output", required=True, help="Output PNG path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    result = render_og_image(
        title=args.title,
        excerpt=args.excerpt,
        date_str=args.date,
        edition_label=args.edition,
        output_path=args.output,
    )
    if result is None:
        print("Failed to generate OG image (Pillow not available?)")
        raise SystemExit(1)
    print(f"OG image written to {args.output}")


if __name__ == "__main__":
    main()