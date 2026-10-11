"""
Subtitle-file parsing (SRT / VTT)
=================================
One parser for the caption files that YouTube / the user supply. Engines 2 and 3 each carried
their own copy (with slightly different clean-up); both now call this and only adapt the result
to their own segment layout.
"""

import re
from typing import List, Tuple

from core.subtitle_builder import parse_srt_timestamp

_TIMESTAMP_RE = re.compile(r"([\d:,.]+)\s*-->\s*([\d:,.]+)")
_MARKUP_RE = re.compile(r"<[^>]+>")       # <c>, <v Name>, <b> ...
_ASS_TAG_RE = re.compile(r"\{[^}]+\}")    # {\an8} and similar


def parse_subtitle_cues(content: str) -> List[Tuple[float, float, str]]:
    """
    Returns ``[(start_sec, end_sec, text), ...]`` in file order.

    Cues without text are dropped. Cues with a zero or negative duration are kept: whether they
    matter is the caller's decision (YouTube rolling captions contain very short cues).
    """
    cues: List[Tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        arrow = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        if arrow == -1:
            continue
        match = _TIMESTAMP_RE.search(lines[arrow])
        if not match:
            continue
        text = " ".join(lines[arrow + 1:])
        text = _ASS_TAG_RE.sub("", _MARKUP_RE.sub("", text)).strip()
        if not text:
            continue
        cues.append((parse_srt_timestamp(match.group(1)), parse_srt_timestamp(match.group(2)), text))
    return cues


def read_subtitle_cues(path: str) -> List[Tuple[float, float, str]]:
    """``parse_subtitle_cues`` for a file on disk (undecodable bytes are replaced)."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return parse_subtitle_cues(handle.read())
