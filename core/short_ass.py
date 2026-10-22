"""
ASS subtitles for one Short
===========================
A Short is a cut-out of a long video, so its subtitles need two things the studios do not: times counted
from the start of the clip (not of the film), and an optional *hook title* shown at the top for the first
seconds. Pure string building, so the layout rules can be tested without FFmpeg.

Styles come from ``brain.config.SUBTITLE_PRESETS`` (the same looks as the studios). Burmese is shaped by
libass with the bundled Padauk font when the video is rendered with FFmpeg's ``ass`` filter.
"""

from typing import Any, Dict, List, Optional, Sequence

from brain.config import SUBTITLE_PRESETS
from core.subtitle_builder import format_ass_timestamp

FONT_NAME = "Padauk"
HOOK_SECONDS = 3.2
_BREAK_AFTER = (" ", "\u104a", "\u104b", ",", ".", "?", "!")   # places a line may break (Myanmar \u104a / \u104b = , / .)
_MIN_LINE_SECONDS = 0.3

# Per canvas: (width, height, subtitle font size, subtitle bottom margin, hook font size, hook top margin, max chars per line)
GEOMETRY = {
    ("9:16", "1080p"): (1080, 1920, 56, 360, 64, 190, 22),
    ("9:16", "720p"): (720, 1280, 38, 240, 44, 125, 22),
    ("16:9", "1080p"): (1920, 1080, 48, 55, 56, 70, 40),
    ("16:9", "720p"): (1280, 720, 34, 40, 40, 50, 40),
}


def geometry(video_format: str, resolution: str):
    """Canvas size and text metrics for a format / resolution (unknown values fall back to 9:16 1080p)."""
    return GEOMETRY.get((video_format, resolution), GEOMETRY[("9:16", "1080p")])


def wrap_text(text: str, max_chars: int, max_lines: int = 3) -> List[str]:
    """
    Breaks ``text`` into lines of about ``max_chars``, only at spaces and after Myanmar / Latin punctuation, so
    a syllable is never split. Text without a break point stays on one line (libass wraps it itself).
    """
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return [text] if text else []
    lines: List[str] = []
    rest = text
    while len(rest) > max_chars and len(lines) < max_lines - 1:
        cut = -1
        for index in range(min(max_chars, len(rest) - 1), max(max_chars // 2, 1), -1):
            if rest[index - 1] in _BREAK_AFTER or rest[index] == " ":
                cut = index
                break
        if cut <= 0:
            break
        lines.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    lines.append(rest)
    return [line for line in lines if line]


def _escape(text: str) -> str:
    """ASS treats braces as override tags and a backslash as an escape; neither may come from the text."""
    return text.replace("\\", "").replace("{", "").replace("}", "")


def relative_lines(lines: Sequence[Dict[str, Any]], clip_start: float, clip_end: float):
    """(start, end, text) of every line that falls inside the clip, with times counted from the clip start."""
    out = []
    for line in lines:
        text = str(line.get("text") or "").strip()
        start = max(float(line.get("start", 0.0)), clip_start) - clip_start
        end = min(float(line.get("end", 0.0)), clip_end) - clip_start
        if text and end - start >= _MIN_LINE_SECONDS:
            out.append((start, end, text))
    return out


def build_short_ass(lines: Sequence[Dict[str, Any]], clip_start: float, clip_end: float, video_format: str = "9:16",
                    resolution: str = "1080p", preset: str = "box_black", title: Optional[str] = None) -> str:
    """
    The complete ASS document for one clip. ``lines`` are dicts with ``start`` / ``end`` in seconds of the
    source video and the text to show under ``text``; ``title`` (when given) is shown at the top of the
    picture for the first seconds.
    """
    width, height, size, margin_v, hook_size, hook_top, max_chars = geometry(video_format, resolution)
    style = SUBTITLE_PRESETS.get(str(preset).lower(), SUBTITLE_PRESETS["box_black"])
    primary = style.get("primary_color", "&H00FFFFFF")
    outline = style.get("outline_color", "&H00000000")
    back = style.get("back_color", "&HB0000000")
    border = style.get("border_style", 3)
    outline_w = style.get("outline_width", 4)
    shadow = style.get("shadow", 2)

    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\n"
        f"PlayResX: {width}\nPlayResY: {height}\nScaledBorderAndShadow: yes\nCollisions: Normal\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{FONT_NAME},{size},{primary},&H000000FF,{outline},{back},-1,0,0,0,100,100,0,0,"
        f"{border},{outline_w},{shadow},2,40,40,{margin_v},1\n"
        # The hook: gold text, top-centre, always on the dark box so it reads on any picture.
        f"Style: Hook,{FONT_NAME},{hook_size},&H0000D7FF,&H000000FF,&H00000000,&HB0000000,-1,0,0,0,100,100,0,0,"
        f"3,{outline_w},{shadow},8,50,50,{hook_top},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events: List[str] = []
    if title and title.strip():
        shown = min(HOOK_SECONDS, max(1.0, clip_end - clip_start))
        hook = "\\N".join(_escape(part) for part in wrap_text(title, max_chars, max_lines=3))
        events.append(f"Dialogue: 1,{format_ass_timestamp(0.0)},{format_ass_timestamp(shown)},Hook,,0,0,0,,"
                      f"{{\\fad(250,350)}}{hook}\n")
    for start, end, text in relative_lines(lines, clip_start, clip_end):
        shown_text = "\\N".join(_escape(part) for part in wrap_text(text, max_chars))
        events.append(f"Dialogue: 0,{format_ass_timestamp(start)},{format_ass_timestamp(end)},Default,,0,0,0,,{shown_text}\n")
    return header + "".join(events)


def text_lines_for_clip(segments: Sequence[Dict[str, Any]], clip_start: float, clip_end: float,
                        field: str = "burmese") -> List[Dict[str, Any]]:
    """The transcript segments that overlap the clip, with the text of ``field`` (falling back to the original)."""
    chosen = []
    for segment in segments:
        start, end = float(segment.get("start", 0.0)), float(segment.get("end", 0.0))
        if end <= clip_start or start >= clip_end:
            continue
        text = str(segment.get(field) or segment.get("original") or segment.get("text") or "").strip()
        chosen.append({"start": start, "end": end, "text": text})
    return chosen

