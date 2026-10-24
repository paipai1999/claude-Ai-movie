"""
Subtitle Builder & Styling Utility (စာတန်းထိုး တည်ဆောက်မှုနှင့် ဒီဇိုင်းစနစ်)
==========================================================================
Handles generation of ASS (Advanced SubStation Alpha) and SRT (SubRip) subtitles
with support for cinema-grade styling presets, custom font sizing, responsive
aspect ratio dimensions, and natural Burmese syllable wrapping.

ဤ module သည် ASS နှင့် SRT စာတန်းထိုးဖိုင်များကို တည်ဆောက်ပေးပြီး Cinema Box၊
TikTok Yellow စသည့် ဒီဇိုင်းပုံစံများ၊ မြန်မာစာလုံး ပိုင်းဖြတ်မှုများကို စနစ်တကျ စီမံပေးပါသည်။
"""

import sys
from typing import List, Dict, Any
from brain.burmese_utils import (
    strip_trailing_subtitle_punctuation,
    normalize_standard_burmese_spelling,
)
from brain.config import SUBTITLE_PRESETS


def resolve_font_name(font_name: str = "Myanmar Text") -> str:
    """
    Resolves font name for target OS.
    On Linux/Darwin/Docker/Colab/Kaggle, automatically falls back 'Myanmar Text'
    to 'Padauk' (bundled in assets/fonts/Padauk.ttf).
    """
    if not font_name:
        return "Padauk"
    cleaned = str(font_name).strip()
    if sys.platform != "win32" and cleaned in ("Myanmar Text", "Myanmar3", "Pyidaungsu"):
        return "Padauk"
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Subtitle Style Presets (ဒီဇိုင်းပုံစံ အဓိပ္ပာယ်ဖွင့်ဆိုချက်များ)
# ─────────────────────────────────────────────────────────────────────────────
# SINGLE SOURCE OF TRUTH is brain.config.SUBTITLE_PRESETS (also used by the hardsub engine, the
# recap merger and the dashboard preview). This view only renames the fields the ASS builder
# below expects, so the on-screen preview and the rendered subtitles can never drift apart.
def _derive_style_presets() -> Dict[str, Dict[str, Any]]:
    return {
        preset_id: {
            "name": p.get("name", preset_id),
            "description": p.get("desc", ""),
            "primary_color": p["primary_color"],
            "outline_color": p["outline_color"],
            "back_color": p["back_color"],
            "border_style": p["border_style"],
            "outline_w": p["outline_width"],
            "shadow": p["shadow"],
        }
        for preset_id, p in SUBTITLE_PRESETS.items()
    }


SUBTITLE_STYLE_PRESETS: Dict[str, Dict[str, Any]] = _derive_style_presets()


# ─────────────────────────────────────────────────────────────────────────────
# Timecode Formatting Utilities (အချိန်မှတ် အသွင်ပြောင်း ကိရိယာများ)
# ─────────────────────────────────────────────────────────────────────────────
def format_srt_timestamp(seconds: float) -> str:
    """
    Converts a floating-point seconds value into standard SRT timecode (HH:MM:SS,mmm).
    စက္ကန့်ကို စံချိန်မီ SRT အချိန်မှတ် (နာရီ:မိနစ်:စက္ကန့်,မီလီစက္ကန့်) သို့ ပြောင်းပေးသည်။
    """
    total_ms = int(round(max(0.0, float(seconds)) * 1000))
    hours = total_ms // 3_600_000
    minutes = (total_ms % 3_600_000) // 60_000
    secs = (total_ms % 60_000) // 1000
    ms = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def format_ass_timestamp(seconds: Any) -> str:
    """
    Converts a floating-point seconds value (or SRT timestamp string) into ASS timecode (H:MM:SS.cc).
    စက္ကန့် (သို့မဟုတ် SRT အချိန်မှတ် စာသား) ကို စံချိန်မီ ASS အချိန်မှတ် သို့ ပြောင်းပေးသည်။
    """
    if isinstance(seconds, str):
        try:
            seconds = float(seconds)
        except ValueError:
            seconds = parse_srt_timestamp(seconds)
    total_cs = int(round(max(0.0, float(seconds)) * 100))
    hours = total_cs // 360_000
    minutes = (total_cs % 360_000) // 6000
    secs = (total_cs % 6000) // 100
    cs = total_cs % 100
    return f"{hours:01d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def parse_srt_timestamp(ts_str: str) -> float:
    """
    Parses an SRT timestamp string (HH:MM:SS,mmm or HH:MM:SS.mmm) into floating-point seconds.
    SRT အချိန်မှတ် စာသားကို floating-point စက္ကန့်အဖြစ် ပြောင်းလဲတွက်ချက်ပေးသည်။
    """
    clean = str(ts_str).replace(",", ".").strip()
    parts = clean.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return float(h) * 3600.0 + float(m) * 60.0 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return float(m) * 60.0 + float(s)
    return float(clean)


# ─────────────────────────────────────────────────────────────────────────────
# Burmese Text Wrapping Utility (မြန်မာစာကြောင်း အဖြတ်အတောက် စီမံခြင်း)
# ─────────────────────────────────────────────────────────────────────────────
def wrap_burmese_text(text: str, max_chars: int = 32) -> str:
    """
    Wraps long Burmese sentences at natural grammatical break points (၊, ။, or space),
    ensuring line ends do not have trailing ( ၊ , ။ ).
    """
    text = strip_trailing_subtitle_punctuation(text)
    if len(text) <= max_chars or "\\N" in text:
        return text

    mid = len(text) // 2
    split_at = -1
    for offset in range(len(text) // 2):
        for candidate in [mid - offset, mid + offset]:
            if 0 <= candidate < len(text) and text[candidate] in (" ", "၊", "။"):
                split_at = candidate
                break
        if split_at != -1:
            break

    if split_at != -1:
        first_part = strip_trailing_subtitle_punctuation(text[:split_at].strip())
        second_part = strip_trailing_subtitle_punctuation(text[split_at:].strip())
        return first_part + "\\N" + second_part
    return text


# ─────────────────────────────────────────────────────────────────────────────
# ASS Subtitle Script Builder (ASS စာတန်းထိုး ဖိုင်အပြည့်အစုံ ဖန်တီးခြင်း)
# ─────────────────────────────────────────────────────────────────────────────
def build_ass_script(
    segments: List[Dict[str, Any]],
    preset: str = "box_black",
    video_w: int = 1920,
    video_h: int = 1080,
    resolution: str = "1080p",
    font_name: str = "Padauk",
    margin_bottom: int = 40,
    text_key: str = "burmese",
) -> str:
    """
    Builds a complete, formatted ASS subtitle script string.
    ရွေးချယ်ထားသော style preset နှင့် video resolution အလိုက် အပြည့်အစုံ ASS စာသားကို တည်ဆောက်ပေးသည်။
    """
    font_name = resolve_font_name(font_name)

    # 1. Resolve Style Preset Parameters
    style_info = SUBTITLE_STYLE_PRESETS.get(preset, SUBTITLE_STYLE_PRESETS["box_black"])
    primary_color = style_info["primary_color"]
    outline_color = style_info["outline_color"]
    back_color = style_info["back_color"]
    border_style = style_info["border_style"]
    outline_w = style_info["outline_w"]
    shadow = style_info["shadow"]

    # 2. Adjust font size proportionally based on vertical resolution
    is_vertical = video_h > video_w
    if resolution == "1080p":
        font_size = 38 if is_vertical else 42
    elif resolution == "480p":
        font_size = 20 if is_vertical else 22
    else:
        font_size = 28 if is_vertical else 32

    # 3. Build Header and Style definitions
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        f"PlayResX: {video_w}\n"
        f"PlayResY: {video_h}\n"
        "ScaledBorderAndShadow: yes\n"
        "Collisions: Normal\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},{primary_color},&H000000FF,{outline_color},{back_color},"
        f"-1,0,0,0,100,100,0,0,{border_style},{outline_w},{shadow},2,15,15,{margin_bottom},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = [header]

    # 4. Generate dialogue lines with timecodes and wrapping
    for seg in segments:
        raw_val = seg.get(text_key, "").strip()
        norm_val = normalize_standard_burmese_spelling(raw_val)
        txt = strip_trailing_subtitle_punctuation(norm_val)
        if not txt:
            continue

        # Wrap long dialogue cleanly
        wrapped_txt = wrap_burmese_text(txt, max_chars=34 if is_vertical else 42)
        safe_txt = wrapped_txt.replace("{", "").replace("}", "")

        # Compute start and end in seconds
        start_s = seg.get("start_s", seg.get("start", 0.0))
        end_s = seg.get("end_s", seg.get("end", 0.0))
        if isinstance(start_s, str):
            try:
                parts = start_s.replace(",", ".").split(":")
                start_s = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            except Exception:
                start_s = 0.0
        if isinstance(end_s, str):
            try:
                parts = end_s.replace(",", ".").split(":")
                end_s = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            except Exception:
                end_s = start_s + 2.0

        start_ass = format_ass_timestamp(float(start_s))
        end_ass = format_ass_timestamp(float(end_s))

        lines.append(f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{safe_txt}\n")

    return "".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# SRT Subtitle Script Builder (SRT စာတန်းထိုး ဖိုင်အပြည့်အစုံ ဖန်တီးခြင်း)
# ─────────────────────────────────────────────────────────────────────────────
def build_srt_script(
    segments: List[Dict[str, Any]],
    text_key: str = "burmese",
) -> str:
    """
    Builds a standard SubRip (.srt) subtitle file string.
    စံချိန်မီ .srt ဖိုင်စာသားကို တည်ဆောက်ပေးသည်။
    """
    blocks = []
    idx = 1
    for seg in segments:
        raw_val = seg.get(text_key, "").strip()
        norm_val = normalize_standard_burmese_spelling(raw_val)
        txt = strip_trailing_subtitle_punctuation(norm_val)
        if not txt:
            continue

        start_s = seg.get("start_s", seg.get("start", 0.0))
        end_s = seg.get("end_s", seg.get("end", 0.0))
        if isinstance(start_s, str) and "," in start_s:
            start_str = start_s
        else:
            start_str = format_srt_timestamp(float(start_s))

        if isinstance(end_s, str) and "," in end_s:
            end_str = end_s
        else:
            end_str = format_srt_timestamp(float(end_s))

        blocks.append(f"{idx}\n{start_str} --> {end_str}\n{txt}\n")
        idx += 1

    return "\n".join(blocks)


def write_srt_file(
    output_path: str,
    segments_or_content: Any,
    text_key: str = "burmese",
    with_bom: bool = True,
) -> str:
    """
    Writes an SRT subtitle file to disk with UTF-8 BOM encoding for CapCut compatibility.
    CapCut PC/Mobile တွင် မြန်မာစာ ယူနီကုဒ် font များ စာလုံးမပျက်စေရန် UTF-8 with BOM (utf-8-sig) ဖြင့် သိမ်းဆည်းပေးသည်။
    """
    import os
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    if isinstance(segments_or_content, list):
        content = build_srt_script(segments_or_content, text_key=text_key)
    else:
        content = str(segments_or_content)

    encoding = "utf-8-sig" if with_bom else "utf-8"
    with open(output_path, "w", encoding=encoding) as f:
        f.write(content)
    return output_path

