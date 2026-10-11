"""
Speech-segment timing guard
===========================
Whisper segments frequently run on through silence, music or sound effects: a three-second
line can be reported as lasting twenty seconds, which leaves a subtitle stuck on screen.

``tighten_end`` only ever *shortens* a segment. It prefers the end of the last recognised
word (``word_timestamps=True``) and, as a second line of defence, caps the duration to what
the text can plausibly take to speak.
"""

import re
from typing import Iterable, Optional

# Rough speaking speed. CJK scripts carry a lot of meaning per character, Latin text far less.
_CJK_SEC_PER_CHAR = 0.30
_OTHER_SEC_PER_CHAR = 0.075
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯฀-๿]")

MIN_SEGMENT_SEC = 0.3
TRAILING_PAD_SEC = 0.35  # keeps a subtitle on screen briefly after the last word
MIN_CAP_SEC = 2.0
CAP_SLACK = 1.6          # allow slow / dramatic delivery
CAP_PAD_SEC = 1.0


def estimate_speech_seconds(text: str) -> float:
    """Plausible time needed to speak ``text`` (language-agnostic, deliberately generous)."""
    text = (text or "").strip()
    if not text:
        return 0.0
    cjk = len(_CJK_RE.findall(text))
    other = len(re.sub(r"\s+", "", text)) - cjk
    return cjk * _CJK_SEC_PER_CHAR + max(other, 0) * _OTHER_SEC_PER_CHAR


def _word_end(word) -> Optional[float]:
    value = word.get("end") if isinstance(word, dict) else getattr(word, "end", None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def tighten_end(start: float, end: float, text: str, words: Optional[Iterable] = None) -> float:
    """Returns an end time <= ``end`` that does not outlive the speech in the segment."""
    start = float(start)
    end = float(end)
    if end <= start:
        return end

    tightened = end

    # 1) Word-level timestamps: the segment cannot outlast its last spoken word.
    if words:
        ends = [e for e in (_word_end(w) for w in words) if e is not None and e > start]
        if ends:
            tightened = min(tightened, max(ends) + TRAILING_PAD_SEC)

    # 2) Duration cap derived from the amount of text.
    cap = max(MIN_CAP_SEC, estimate_speech_seconds(text) * CAP_SLACK + CAP_PAD_SEC)
    tightened = min(tightened, start + cap)

    return round(max(tightened, start + MIN_SEGMENT_SEC), 3)
