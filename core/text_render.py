"""
Complex-script text rendering (Myanmar-safe)
============================================
Pillow's default ("basic") layout engine cannot shape Myanmar: vowel sign ေ is drawn after the
consonant instead of before it, medials and ူ/ု detach from their base. Pillow only shapes
correctly with libraqm + libfribidi, which are missing on the stock Windows wheel.

FFmpeg's ``drawtext`` filter runs HarfBuzz, and the FFmpeg we already ship for subtitles (libass)
has it, so rendering the glyphs through FFmpeg gives correct Burmese with no extra dependency:

    render_text(...)   -> tightly cropped RGBA image of one line (None if it could not be shaped)
    render_lines(...)  -> several lines stacked and aligned
    draw_text(...)     -> best available renderer, always returns an image

``draw_text`` degrades to Pillow's own renderer, so a broken FFmpeg never loses the card/watermark.
"""

import os
import subprocess
import tempfile
from typing import TYPE_CHECKING, Optional, Sequence, Tuple

from brain.diagnostics import log_suppressed

if TYPE_CHECKING:  # Pillow stays a lazy import at runtime
    from PIL import Image

RGBA = Tuple[int, int, int, int]

_MAX_CANVAS = 8192            # px – keeps a pathological text from allocating a giant frame
_RENDER_TIMEOUT_SEC = 30


def _escape_filter_value(value: str) -> str:
    """Escape a path/string for use inside a single-quoted FFmpeg filter option."""
    return value.replace("\\", "/").replace("'", r"\'").replace(":", r"\:")


def pillow_shapes_complex_scripts() -> bool:
    """True when Pillow itself can shape Myanmar (libraqm + fribidi present)."""
    try:
        from PIL import features
        return bool(features.check("raqm"))
    except Exception as exc:
        log_suppressed("core.text_render.pillow_shapes_complex_scripts", exc)
        return False


def render_text(
    ffmpeg_bin: str,
    text: str,
    font_path: str,
    font_size: int,
    fill: RGBA = (255, 255, 255, 255),
) -> Optional["Image.Image"]:
    """
    One line of text as an RGBA image cropped to the ink, shaped by FFmpeg/HarfBuzz.
    Returns None for empty text, a missing font/FFmpeg, or any render failure.
    """
    text = (text or "").replace("\r", "").replace("\n", " ").strip()
    if not text or not ffmpeg_bin or not font_path or not os.path.exists(font_path):
        return None

    from PIL import Image

    size = max(8, int(font_size))
    pad = size // 2
    width = min(_MAX_CANVAS, max(64, int(len(text) * size * 1.4) + pad * 2))
    height = int(size * 2.4)

    # textfile= avoids every shell/filtergraph quoting problem with arbitrary user text.
    txt_fd, txt_path = tempfile.mkstemp(suffix=".txt", prefix="pai_text_")
    png_fd, png_path = tempfile.mkstemp(suffix=".png", prefix="pai_text_")
    os.close(png_fd)
    try:
        with os.fdopen(txt_fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        vf = (
            f"drawtext=fontfile='{_escape_filter_value(os.path.abspath(font_path))}':"
            f"textfile='{_escape_filter_value(txt_path)}':fontsize={size}:fontcolor=white:"
            f"x={pad}:y=(h-text_h)/2:text_shaping=1:expansion=none"
        )
        cmd = [
            ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}",
            "-vf", vf, "-frames:v", "1", png_path,
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=_RENDER_TIMEOUT_SEC)
        if res.returncode != 0 or not os.path.exists(png_path) or os.path.getsize(png_path) == 0:
            return None

        # White-on-black coverage becomes the alpha channel of a solid-colour layer.
        with Image.open(png_path) as raw:
            mask = raw.convert("L")
        bbox = mask.getbbox()
        if not bbox:
            return None
        margin = max(1, size // 12)
        box = (
            max(0, bbox[0] - margin), max(0, bbox[1] - margin),
            min(mask.width, bbox[2] + margin), min(mask.height, bbox[3] + margin),
        )
        mask = mask.crop(box)
        if fill[3] < 255:
            alpha = fill[3]
            mask = mask.point(lambda p: p * alpha // 255)
        layer = Image.new("RGBA", mask.size, (fill[0], fill[1], fill[2], 0))
        layer.putalpha(mask)
        return layer
    except Exception as exc:
        log_suppressed("core.text_render.render_text", exc)
        return None
    finally:
        for path in (txt_path, png_path):
            try:
                os.remove(path)
            except OSError:
                pass


def _pillow_line(text: str, font_path: str, font_size: int, fill: RGBA):
    """Fallback renderer: Pillow's own text drawing (correct for Latin, approximate for Myanmar)."""
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype(font_path, font_size)
    except Exception:
        font = ImageFont.load_default()
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
    layer = Image.new("RGBA", (max(1, right - left), max(1, bottom - top)), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((-left, -top), text, font=font, fill=fill)
    return layer


def draw_text(
    ffmpeg_bin: str,
    text: str,
    font_path: str,
    font_size: int,
    fill: RGBA = (255, 255, 255, 255),
):
    """
    One line of text as a cropped RGBA image using the best renderer available:
    Pillow when it can shape complex scripts itself, otherwise FFmpeg/HarfBuzz, otherwise
    Pillow's basic layout (a mis-shaped Myanmar line is still better than a missing one).
    """
    text = (text or "").replace("\r", "").replace("\n", " ").strip()
    if not text:
        return None
    if not pillow_shapes_complex_scripts():
        shaped = render_text(ffmpeg_bin, text, font_path, font_size, fill)
        if shaped is not None:
            return shaped
    return _pillow_line(text, font_path, font_size, fill)


def render_lines(
    ffmpeg_bin: str,
    lines: Sequence[str],
    font_path: str,
    font_size: int,
    fill: RGBA = (255, 255, 255, 255),
    *,
    spacing: int = 12,
    align: str = "center",
):
    """Several lines stacked into one RGBA image (``align``: left | center | right)."""
    parts = [img for img in (draw_text(ffmpeg_bin, ln, font_path, font_size, fill) for ln in lines) if img]
    if not parts:
        return None
    from PIL import Image

    total_w = max(p.width for p in parts)
    total_h = sum(p.height for p in parts) + spacing * (len(parts) - 1)
    canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    y = 0
    for part in parts:
        if align == "left":
            x = 0
        elif align == "right":
            x = total_w - part.width
        else:
            x = (total_w - part.width) // 2
        canvas.paste(part, (x, y), part)
        y += part.height + spacing
    return canvas
