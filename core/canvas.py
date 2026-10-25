"""
Putting a picture on a canvas of another shape
==============================================
A part of a long video may be wanted as a vertical (9:16), square (1:1) or landscape (16:9) video while the source is
something else. A plain scale would squash it, so the picture is placed on the canvas in one of three ways:

    blur    the whole picture, fitted inside the canvas over a blurred copy of itself (nothing is cut away)
    black   the whole picture, fitted inside the canvas between black bars
    crop    the canvas is filled from the middle of the picture (the edges are cut away)

Pure string building (a filter-graph fragment), so the rules can be tested without FFmpeg. The Splitter and
Long to Short share it.
"""
import os
from typing import Tuple

from core import resolution as res_presets
from core.letterbox import plan_canvas_mode

FORMATS = ("original", "9:16", "1:1", "16:9")
FILLS = ("blur", "black", "crop")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = os.path.join(PROJECT_ROOT, "assets", "fonts")


def escape_filter_path(value: str) -> str:
    """A path inside an FFmpeg filter argument: forward slashes, with quotes and the drive colon escaped."""
    return value.replace("\\", "/").replace("'", r"\'").replace(":", r"\:")


def ass_filter(ass_name: str) -> str:
    """The ``ass`` filter for a subtitle file in the working directory (libass shapes Myanmar with the bundled font)."""
    return f"ass=filename='{escape_filter_path(ass_name)}':shaping=1:fontsdir='{escape_filter_path(FONTS_DIR)}'"


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def canvas_for(video_format: str, resolution, src_w: int, src_h: int) -> Tuple[int, int]:
    """
    (width, height) of the output. "original" keeps the shape of the source (never larger than the quality preset
    allows); the other formats take the size of the preset.
    """
    if video_format == "original":
        try:
            src_w, src_h = int(src_w), int(src_h)
        except (TypeError, ValueError):
            src_w = src_h = 0
        if src_w <= 0 or src_h <= 0:
            return res_presets.canvas("16:9", resolution)
        factor = min(1.0, res_presets.short_side(resolution) / min(src_w, src_h))
        return _even(src_w * factor), _even(src_h * factor)
    return res_presets.canvas(video_format, resolution)


def blur_band(start_y: float, height: float, source: str = "[0:v]", label: str = "[cv_clean]", radius: int = 18) -> str:
    """
    The filter-graph fragment that blurs a horizontal band of ``source`` (``start_y`` and ``height`` as fractions of the
    picture, like the Vision AI reports them) - the burnt-in subtitles of the source, so that the Burmese ones laid
    over them stay readable. Output label ``label``.
    """
    start_y = max(0.0, min(0.95, float(start_y)))
    height = max(0.02, min(1.0 - start_y, float(height)))
    return (f"{source}split=2[cv_band_src][cv_band_crop];"
            f"[cv_band_crop]crop=iw:'trunc(ih*{height:.3f}/2)*2':0:'trunc(ih*{start_y:.3f}/2)*2',"
            f"boxblur=luma_radius={radius}:luma_power=2:chroma_radius={max(1, radius // 2)}:chroma_power=2[cv_band_blur];"
            f"[cv_band_src][cv_band_blur]overlay=0:'trunc(H*{start_y:.3f}/2)*2'{label}")


def graph(src_w, src_h, out_w: int, out_h: int, fill: str = "blur", source: str = "[0:v]", label: str = "[canvas]") -> str:
    """
    The filter-graph fragment that turns ``source`` into an ``out_w`` x ``out_h`` picture with output label ``label``.
    A source that already has the canvas's shape is simply scaled; an unknown source size is treated the same way.
    """
    if plan_canvas_mode(src_w, src_h, out_w, out_h) == "direct":
        return f"{source}scale={out_w}:{out_h}:flags=lanczos,setsar=1{label}"
    if fill == "crop":
        return (f"{source}scale={out_w}:{out_h}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={out_w}:{out_h},setsar=1{label}")
    if fill == "black":
        return (f"{source}scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
                f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1{label}")
    bg_w, bg_h = max(2, out_w // 4), max(2, out_h // 4)
    return (f"{source}split=2[cv_bg_src][cv_fg_src];"
            f"[cv_bg_src]scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,crop={bg_w}:{bg_h},boxblur=12:3,"
            f"scale={out_w}:{out_h}[cv_bg];"
            f"[cv_fg_src]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[cv_fg];"
            f"[cv_bg][cv_fg]overlay=(W-w)/2:(H-h)/2,setsar=1{label}")
