"""
Speech-to-text sanity checks
============================
Whisper sometimes falls into a repetition loop on music, silence or noise and "hears" the same
sentence dozens of times ("Thank you." x 60). That junk used to flow straight into translation and
voice-over, burning Gemini quota and producing a narrated video about nothing.

``clean_transcript`` runs right after Whisper, before any API spend:

* drops the 3rd+ copy of a meaningful line that already appeared in the last few lines,
* drops well-known Whisper credit artefacts ("Subtitles by the Amara.org community"),
* flags the transcript ``unusable`` when almost nothing real is left, so the engine can stop with a
  clear message instead of dubbing noise.

It works on plain dicts (``{"start", "end", "text"}``) and on objects with a ``.text`` attribute, and
returns the same items it was given (filtered) - callers keep their own segment type.
"""

import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, List, Sequence, Tuple

from core.text_compare import normalize

# A line counts as "meaningful" when it has this many words or letters - one-word answers such as
# "Yes." or "No." repeat legitimately and must never be treated as a loop.
_MIN_WORDS = 3
_MIN_LETTERS = 6
_WINDOW = 8             # how many previous lines a repeat is looked up in
_ALLOWED_COPIES = 2     # "A A" is natural speech; the third copy inside the window is a loop
_MIN_LINES_TO_JUDGE = 12
_USABLE_FRACTION = 0.25  # fewer survivors than this share of a long transcript => unusable

_ARTEFACT = re.compile(
    r"amara\.org|subtitles?\s+(?:by|made\s+by)\b|subtitled\s+by\b|captions?\s+by\b|字幕|자막\s*제공|"
    r"translated\s+by\b|www\.\S+\.\S+",
    re.IGNORECASE,
)
_ARTEFACT_MAX_CHARS = 90  # credits are short; a long line that merely mentions a site is real speech


@dataclass
class SpeechReport:
    total: int = 0
    kept: int = 0
    looped: int = 0      # dropped repeats
    artefacts: int = 0   # dropped credit lines
    unusable: bool = False

    @property
    def dropped(self) -> int:
        return self.looped + self.artefacts

    def summary(self) -> str:
        return (
            f"{self.total} segments heard, {self.kept} kept "
            f"({self.looped} repeated lines and {self.artefacts} credit lines removed)"
        )


def _text_of(seg: Any) -> str:
    if isinstance(seg, dict):
        return str(seg.get("text", "") or "")
    return str(getattr(seg, "text", "") or "")


normalize_line = normalize   # kept as the public name this module's callers/tests use


def is_meaningful(norm: str) -> bool:
    if not norm:
        return False
    return len(norm.split()) >= _MIN_WORDS or len(norm.replace(" ", "")) >= _MIN_LETTERS


def is_artefact(text: str) -> bool:
    text = text.strip()
    return bool(text) and len(text) <= _ARTEFACT_MAX_CHARS and bool(_ARTEFACT.search(text))


def clean_transcript(segments: Sequence[Any]) -> Tuple[List[Any], SpeechReport]:
    """Returns ``(kept_segments, report)``; the input sequence is never modified."""
    report = SpeechReport(total=len(segments))
    kept: List[Any] = []
    recent: Deque[str] = deque(maxlen=_WINDOW)   # normalised lines seen lately, dropped ones included

    for seg in segments:
        text = _text_of(seg)
        if is_artefact(text):
            report.artefacts += 1
            continue
        norm = normalize_line(text)
        if is_meaningful(norm):
            if sum(1 for prev in recent if prev == norm) >= _ALLOWED_COPIES:
                report.looped += 1
                recent.append(norm)   # keep counting so a long loop stays suppressed
                continue
            recent.append(norm)
        else:
            recent.append("")        # short answers neither start nor extend a loop
        kept.append(seg)

    report.kept = len(kept)
    report.unusable = bool(
        report.total >= _MIN_LINES_TO_JUDGE and report.kept < report.total * _USABLE_FRACTION
    )
    return kept, report


def clean_and_report(segments: Sequence[Any], log: Callable[[str], None] = print) -> List[Any]:
    """
    ``clean_transcript`` plus the log lines every engine wants. Returns ``[]`` for an unusable
    transcript so the caller's existing "no transcript, stop" path takes over.
    """
    kept, report = clean_transcript(segments)
    if report.dropped:
        log(f"[*] Transcript check: {report.summary()}.")
    if report.unusable:
        log(
            "[WARN] The speech-to-text result is almost entirely repeated lines - Whisper most likely "
            "looped on music/noise or the wrong source language is set. / "
            "အသံမှတ်သားမှု ရလဒ်က စာကြောင်းတူတွေ ထပ်နေပါတယ် - ဇာတ်ကားမူလဘာသာစကား ပြန်စစ်ပါ၊ "
            "Whisper model ကို ပိုကြီးတာ (medium/large) သုံးပါ၊ ဒါမှမဟုတ် .srt ဖိုင် တွဲပေးပါ။"
        )
        return []
    return kept
