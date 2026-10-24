"""
The label on every part of a split video ("Part 2/5")
=====================================================
A series cut into parts is easier to follow when each part says which one it is. The label is a template the user
writes, with these places to fill in:

    {n}        the number of this part:  1, 2, 3
    {mm}       the same in Myanmar digits:  ၁, ၂, ၃
    {total}    how many parts there are:  5
    {total_mm} the same in Myanmar digits:  ၅

The label is drawn with libass (the ``ass`` filter and the bundled Padauk font), which shapes Burmese correctly.
Pure string building, so it can be tested without FFmpeg.
"""
from typing import Optional

from core.short_ass import FONT_NAME, escape_text, wrap_text
from core.subtitle_builder import format_ass_timestamp

MYANMAR_DIGITS = "၀၁၂၃၄၅၆၇၈၉"
MAX_LABEL_CHARS = 80


def myanmar_number(number: int) -> str:
    """12 -> ၁၂"""
    return "".join(MYANMAR_DIGITS[int(d)] for d in str(max(0, int(number))))


def render(template: Optional[str], number: int, total: int) -> str:
    """The label of part ``number`` of ``total``; "" when there is no template. Unknown {words} are left as they are."""
    text = " ".join(str(template or "").split())[:MAX_LABEL_CHARS]
    if not text:
        return ""
    for name, value in (("{n}", str(number)), ("{mm}", myanmar_number(number)),
                        ("{total}", str(total)), ("{total_mm}", myanmar_number(total))):
        text = text.replace(name, value)
    return text


def build_label_ass(text: str, duration: float, width: int, height: int) -> str:
    """
    An ASS document that shows ``text`` for the whole part, top-centre on a dark box (readable on any picture).
    The size follows the short side of the canvas, so the label looks the same on 1080p, 720p and 480p.
    """
    short = min(width, height)
    size = max(14, int(short * 0.05))
    top = max(8, int(height * 0.035))
    per_line = max(10, int(width * 0.8 / (size * 0.62)))
    shown = "\\N".join(escape_text(line) for line in wrap_text(text, per_line, max_lines=2))
    box = max(4, size // 5)
    return (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\n"
        f"PlayResX: {width}\nPlayResY: {height}\nScaledBorderAndShadow: yes\nCollisions: Normal\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Label,{FONT_NAME},{size},&H00FFFFFF,&H000000FF,&H00000000,&HB0000000,-1,0,0,0,100,100,0,0,"
        f"3,{box},0,8,40,40,{top},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,{format_ass_timestamp(0.0)},{format_ass_timestamp(max(1.0, duration) + 1.0)},Label,,0,0,0,,{shown}\n"
    )
