"""Generate Humble Upgrade quote image cards with clear current PFPs."""
from __future__ import annotations

import io
import logging
import random
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("friend-bot.quote_card")

WIDTH = 1200
HEIGHT = 630
LEFT_WIDTH = 520
WATERMARK = "Humble Upgrade"
MAX_QUOTE_CHARS = 280

_FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path(r"C:\Windows\Fonts\segoeui.ttf"),
    Path(r"C:\Windows\Fonts\calibri.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/System/Library/Fonts/Helvetica.ttc"),
]
_FONT_ITALIC_CANDIDATES = [
    Path(r"C:\Windows\Fonts\ariali.ttf"),
    Path(r"C:\Windows\Fonts\segoeuii.ttf"),
    Path(r"C:\Windows\Fonts\calibrii.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf"),
]


def _find_font(candidates: list[Path]) -> Optional[Path]:
    for p in candidates:
        if p.exists():
            return p
    return None


def _load_font(size: int, italic: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _find_font(_FONT_ITALIC_CANDIDATES if italic else _FONT_CANDIDATES)
    if path is None:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(str(path), size=size)
    except OSError:
        return ImageFont.load_default()


def _make_brick_wall(width: int, height: int) -> Image.Image:
    """Subtle dark brick texture for left-panel background only."""
    rng = random.Random(42)
    img = Image.new("L", (width, height), 18)
    draw = ImageDraw.Draw(img)

    brick_h, brick_w = 28, 64
    mortar = 3
    y = 0
    row = 0
    while y < height:
        offset = (brick_w // 2) if row % 2 else 0
        x = -offset
        while x < width:
            shade = 22 + rng.randint(0, 12)
            draw.rectangle(
                [x + mortar, y + mortar, x + brick_w - 1, y + brick_h - 1],
                fill=shade,
            )
            x += brick_w
        y += brick_h
        row += 1

    return img.convert("RGB")


def _initials(display_name: str) -> str:
    parts = [p for p in (display_name or "").strip().split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _circular_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    return mask


def _placeholder_avatar(display_name: str, size: int) -> Image.Image:
    """Simple colored circle with initials when no avatar bytes are available."""
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(bg)
    # Warm orange accent to match Humble Upgrade branding
    draw.ellipse((0, 0, size - 1, size - 1), fill=(232, 120, 40, 255))
    font = _load_font(max(48, size // 3))
    text = _initials(display_name)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((size - tw) // 2 - bbox[0], (size - th) // 2 - bbox[1]),
        text,
        font=font,
        fill=(255, 255, 255, 255),
    )
    return bg


def _prepare_avatar_circle(avatar_bytes: Optional[bytes], display_name: str, size: int) -> Image.Image:
    """
    Full-color circular avatar, or initials placeholder if bytes missing/unreadable.
    Loads via RGBA so WEBP/PNG with alpha are safe.
    """
    av: Optional[Image.Image] = None
    if avatar_bytes:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
        except Exception:
            log.exception("Failed to open avatar image bytes")
            av = None

    if av is None:
        return _placeholder_avatar(display_name, size)

    # Fit square then circular crop — keep full color, no greyscale
    side = min(av.size)
    left = (av.width - side) // 2
    top = (av.height - side) // 2
    av = av.crop((left, top, left + side, top + side))
    av = av.resize((size, size), Image.Resampling.LANCZOS)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(av, (0, 0), _circular_mask(size))
    return out


def _wrap_quote(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    text = " ".join(text.split())
    if len(text) > MAX_QUOTE_CHARS:
        text = text[: MAX_QUOTE_CHARS - 1].rstrip() + "…"

    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            while True:
                bbox = draw.textbbox((0, 0), word, font=font)
                if bbox[2] - bbox[0] <= max_width or len(word) < 2:
                    current = word
                    break
                lo, hi = 1, len(word)
                cut = 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    bb = draw.textbbox((0, 0), word[:mid] + "…", font=font)
                    if bb[2] - bb[0] <= max_width:
                        cut = mid
                        lo = mid + 1
                    else:
                        hi = mid - 1
                lines.append(word[:cut] + "…")
                word = word[cut:]
                current = word
    if current:
        lines.append(current)

    max_lines = 8
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        if not last.endswith("…"):
            lines[-1] = (last[:-1] if len(last) > 1 else last) + "…"
    return lines


def build_quote_card(
    quote_text: str,
    display_name: str,
    username: str,
    avatar_bytes: Optional[bytes] = None,
) -> bytes:
    """
    Build a 1200x630 quote card PNG.

    Left: large clear full-color circular current avatar on dark panel.
    Right: quote / - Name / @handle / Humble Upgrade watermark.
    """
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))

    # --- Left panel: subtle brick + dominant circular PFP ---
    left = _make_brick_wall(LEFT_WIDTH, HEIGHT)
    # Soft darken so brick stays background-only
    left = Image.blend(left, Image.new("RGB", left.size, (0, 0, 0)), 0.35)

    avatar_size = int(min(LEFT_WIDTH, HEIGHT) * 0.72)
    circle = _prepare_avatar_circle(avatar_bytes, display_name, avatar_size)

    # Thin light ring so the PFP reads clearly on dark brick
    ring = Image.new("RGBA", (avatar_size + 10, avatar_size + 10), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse((0, 0, avatar_size + 9, avatar_size + 9), outline=(220, 220, 220, 200), width=3)
    ring.paste(circle, (5, 5), circle)

    px = (LEFT_WIDTH - ring.width) // 2
    py = (HEIGHT - ring.height) // 2
    left_rgba = left.convert("RGBA")
    left_rgba.paste(ring, (px, py), ring)
    canvas.paste(left_rgba.convert("RGB"), (0, 0))

    # Soft vertical divider into black right panel
    fade_w = 48
    fade = Image.new("RGBA", (fade_w, HEIGHT), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fade)
    for x in range(fade_w):
        a = int(255 * (x / max(1, fade_w - 1)))
        fd.line([(x, 0), (x, HEIGHT)], fill=(0, 0, 0, a))
    canvas_rgba = canvas.convert("RGBA")
    canvas_rgba.paste(fade, (LEFT_WIDTH - fade_w // 2, 0), fade)
    canvas = canvas_rgba.convert("RGB")

    # --- Right panel text ---
    draw = ImageDraw.Draw(canvas)
    quote_font = _load_font(44)
    name_font = _load_font(28, italic=True)
    handle_font = _load_font(20)
    wm_font = _load_font(16)

    right_left = LEFT_WIDTH + 28
    right_right = WIDTH - 40
    text_max_w = right_right - right_left

    q = (quote_text or "").strip() or "…"
    size = 44
    lines: list[str] = []
    for size in (48, 44, 40, 36, 32, 28, 24):
        quote_font = _load_font(size)
        lines = _wrap_quote(q, quote_font, text_max_w, draw)
        line_h = size + 10
        block_h = len(lines) * line_h + 28 + 24
        if block_h <= HEIGHT - 80:
            break

    line_h = size + 10
    name_h = 34
    handle_h = 26
    gap_quote_name = 22
    gap_name_handle = 6
    block_h = len(lines) * line_h + gap_quote_name + name_h + gap_name_handle + handle_h
    y = (HEIGHT - block_h) // 2

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=quote_font)
        tw = bbox[2] - bbox[0]
        x = right_left + (text_max_w - tw) // 2
        draw.text((x, y), line, font=quote_font, fill=(255, 255, 255))
        y += line_h

    y += gap_quote_name
    name_text = f"- {display_name}".strip()
    bbox = draw.textbbox((0, 0), name_text, font=name_font)
    tw = bbox[2] - bbox[0]
    x = right_left + (text_max_w - tw) // 2
    draw.text((x, y), name_text, font=name_font, fill=(240, 240, 240))
    y += name_h + gap_name_handle

    handle = username.strip()
    if handle and not handle.startswith("@"):
        handle = f"@{handle}"
    bbox = draw.textbbox((0, 0), handle, font=handle_font)
    tw = bbox[2] - bbox[0]
    x = right_left + (text_max_w - tw) // 2
    draw.text((x, y), handle, font=handle_font, fill=(160, 160, 160))

    bbox = draw.textbbox((0, 0), WATERMARK, font=wm_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        (WIDTH - tw - 18, HEIGHT - th - 14),
        WATERMARK,
        font=wm_font,
        fill=(70, 70, 70),
    )

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def build_quote_card_file(
    quote_text: str,
    display_name: str,
    username: str,
    avatar_bytes: Optional[bytes] = None,
    filename: str = "quote.png",
) -> tuple[io.BytesIO, str]:
    """Return (BytesIO, filename) ready for discord.File."""
    data = build_quote_card(quote_text, display_name, username, avatar_bytes)
    bio = io.BytesIO(data)
    bio.seek(0)
    return bio, filename
