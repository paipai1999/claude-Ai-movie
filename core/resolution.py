"""
Output quality presets and everything that is derived from them (canvas size, subtitle metrics).

One place knows what "1080p", "720p" and "480p" mean, so the engines, the Reels exporter and the
subtitle builders stay in step when a preset is added. "480p" is the *draft* quality: about a
fifth of the pixels of 1080p, so a preview renders many times faster on a modest PC.

ထွက်ရှိမည့် video quality (1080p / 720p / 480p) နှင့် ၎င်းမှ ဆင်းသက်သော အရွယ်အစားများကို ဤနေရာတစ်ခုတည်းတွင် သတ်မှတ်ထားသည်။
"""
from typing import Tuple

DEFAULT = "1080p"
PRESETS = ("1080p", "720p", "480p")
DRAFT = "480p"

# Short side of the canvas in pixels.
_SHORT_SIDE = {"1080p": 1080, "720p": 720, "480p": 480}

# Long side. 480p uses 854 (16:9 rounded up to an even number, which H.264 needs).
_LONG_SIDE = {"1080p": 1920, "720p": 1280, "480p": 854}

# (font size, bottom margin) of the burnt-in narration subtitle on the canvas of each preset.
_SUBTITLE = {
    ("9:16", "1080p"): (48, 140), ("9:16", "720p"): (36, 95), ("9:16", "480p"): (25, 63),
    ("16:9", "1080p"): (48, 55), ("16:9", "720p"): (36, 40), ("16:9", "480p"): (25, 25),
}


def normalize(value, default: str = DEFAULT) -> str:
    """"480", "480P" and " 480p " all become "480p"; anything that is not a preset becomes ``default``."""
    text = str(value or "").strip().lower()
    if text.isdigit():
        text += "p"
    return text if text in _SHORT_SIDE else default


def is_draft(value) -> bool:
    return normalize(value) == DRAFT


def short_side(value) -> int:
    """Pixels of the short side: 1080, 720 or 480."""
    return _SHORT_SIDE[normalize(value)]


def canvas(video_format: str, resolution) -> Tuple[int, int]:
    """(width, height) of the output: portrait for "9:16", square for "1:1", landscape for anything else."""
    preset = normalize(resolution)
    short, long_ = _SHORT_SIDE[preset], _LONG_SIDE[preset]
    if video_format == "1:1":
        return short, short
    return (short, long_) if video_format == "9:16" else (long_, short)


def subtitle_metrics(video_format: str, resolution) -> Tuple[int, int, int, int]:
    """(canvas width, canvas height, font size, bottom margin) for burnt-in subtitles."""
    width, height = canvas(video_format, resolution)
    fmt = "9:16" if video_format == "9:16" else "16:9"
    font, margin = _SUBTITLE[(fmt, normalize(resolution))]
    return width, height, font, margin
