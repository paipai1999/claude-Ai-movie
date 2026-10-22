"""
Long -> Short: choosing the clips
=================================
Pure logic (no model calls, no FFmpeg) behind the "Long to Short" tool, so every rule is testable:

* ``chunk_segments`` / ``format_transcript``  how a long transcript is shown to the model in pieces;
* ``normalize_candidate``  turns one model answer into a usable clip: times parsed, snapped to sentence
  boundaries, forced inside the length limits, score and text fields cleaned;
* ``choose_clips``  keeps the best non-overlapping candidates, in time order;
* ``heuristic_candidates``  an offline fallback (speech density, questions, numbers, strong words) so the
  tool still produces something useful when no Gemini key works.

A transcript segment is a dict with ``start`` / ``end`` in seconds and the spoken text under
``original`` (the studio engines) or ``text``.
"""

import bisect
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

SNAP_TOLERANCE_SEC = 3.0     # a model time this close to a sentence edge moves onto it
MAX_OVERLAP = 0.2            # two clips may share at most this share of the shorter one
CHUNK_SEC = 720.0            # transcript shown to the model in 12-minute pieces ...
CHUNK_OVERLAP_SEC = 45.0     # ... each also showing the first 45 s of the next, so no moment is cut in half
MAX_TITLE_CHARS = 90
MAX_CAPTION_CHARS = 400
MAX_HASHTAGS = 8

FOCUSES = ("balanced", "funny", "emotional", "informative", "action")

_TIME = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[.,](\d+))?\s*$")
_SENTENCE_END = (".", "?", "!", "。", "？", "！", "။")   # ... Myanmar "။" (U+104B)

_STRONG_WORDS = ("secret", "never", "shocking", "unbelievable", "mistake", "truth", "warning", "why", "how",
                 "best", "worst", "free", "million", "crazy", "amazing", "incredible", "suddenly", "finally")
_FOCUS_WORDS = {
    "funny": ("haha", "lol", "joke", "funny", "laugh", "hilarious", "prank"),
    "emotional": ("love", "cry", "tears", "miss", "sorry", "family", "mother", "father", "heart", "forgive"),
    "informative": ("because", "reason", "how to", "step", "tip", "learn", "important", "remember", "example"),
    "action": ("run", "fight", "kill", "fire", "shoot", "attack", "chase", "escape", "explode", "crash"),
}


# ── segment helpers ──────────────────────────────────────────────────────────
def text_of(segment: Dict[str, Any]) -> str:
    return str(segment.get("original") or segment.get("text") or "").strip()


def _start(segment: Dict[str, Any]) -> float:
    return float(segment.get("start", 0.0))


def _end(segment: Dict[str, Any]) -> float:
    return float(segment.get("end", _start(segment)))


def clock(seconds: float) -> str:
    """``mm:ss`` (or ``h:mm:ss`` from one hour up)."""
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse_time(value: Any) -> Optional[float]:
    """Seconds from ``12.5``, ``"12.5"``, ``"01:02"``, ``"1:02:03"`` or ``"00:01:02,500"``; None when unusable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    match = _TIME.match(text)
    if match:
        hours, minutes, seconds, fraction = match.groups()
        total = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
        return total + (float("0." + fraction) if fraction else 0.0)
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _nearest(value: float, points: Sequence[float], tolerance: float) -> float:
    """The point closest to ``value`` when it is within ``tolerance`` (``points`` sorted), else ``value``."""
    if not points:
        return value
    pos = bisect.bisect_left(points, value)
    best = value
    best_gap = tolerance + 1e-9
    for index in (pos - 1, pos):
        if 0 <= index < len(points):
            gap = abs(points[index] - value)
            if gap < best_gap:
                best, best_gap = points[index], gap
    return best


# ── showing the transcript to the model ──────────────────────────────────────
def chunk_segments(segments: Sequence[Dict[str, Any]], chunk_sec: float = CHUNK_SEC,
                   overlap_sec: float = CHUNK_OVERLAP_SEC) -> List[List[Dict[str, Any]]]:
    """The transcript in time pieces; each piece also carries the first ``overlap_sec`` of the next one."""
    if not segments:
        return []
    last = max(_end(s) for s in segments)
    chunks: List[List[Dict[str, Any]]] = []
    low = 0.0
    while low < last:
        part = [s for s in segments if _end(s) > low and _start(s) < low + chunk_sec + overlap_sec]
        if part:
            chunks.append(part)
        low += chunk_sec
    return chunks


def format_transcript(segments: Sequence[Dict[str, Any]], max_chars: int = 300) -> str:
    """One ``[mm:ss-mm:ss] text`` line per segment (the form the model reads; both times so it can pick clean ends)."""
    lines = []
    for segment in segments:
        text = " ".join(text_of(segment).split())
        if text:
            lines.append(f"[{clock(_start(segment))}-{clock(_end(segment))}] {text[:max_chars]}")
    return "\n".join(lines)


# ── cleaning one candidate ───────────────────────────────────────────────────
def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _clean_hashtags(value: Any) -> List[str]:
    items = value if isinstance(value, (list, tuple)) else re.split(r"[\s,]+", str(value or ""))
    tags: List[str] = []
    for item in items:
        tag = str(item).strip().lstrip("#").replace(" ", "")
        if tag and f"#{tag}" not in tags:
            tags.append(f"#{tag}")
    return tags[:MAX_HASHTAGS]


def _clean_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 50.0
    if not math.isfinite(score):
        return 50.0
    if 0 < score <= 1:                     # a model that answered 0.0-1.0 instead of 0-100
        score *= 100.0
    return round(min(100.0, max(0.0, score)), 1)


def normalize_candidate(item: Any, segments: Sequence[Dict[str, Any]], duration: float,
                        min_sec: float, max_sec: float) -> Optional[Dict[str, Any]]:
    """
    One usable clip from a model answer item, or None. Times are snapped to the nearest sentence start / end
    (so a clip never begins or ends mid-word) and forced into ``[min_sec, max_sec]``.
    """
    if not isinstance(item, dict):
        return None
    start, end = parse_time(item.get("start")), parse_time(item.get("end"))
    if start is None or end is None:
        return None
    if end < start:
        start, end = end, start
    duration = float(duration or 0.0)
    if duration > 0:
        start, end = min(start, duration), min(end, duration)

    starts = sorted(_start(s) for s in segments)
    ends = sorted(_end(s) for s in segments)
    start = _nearest(start, starts, SNAP_TOLERANCE_SEC)
    end = _nearest(end, ends, SNAP_TOLERANCE_SEC)
    if end <= start:
        return None

    if end - start > max_sec:                      # too long: stop at the last sentence end inside the limit
        inside = [e for e in ends if start + min_sec <= e <= start + max_sec]
        end = max(inside) if inside else start + max_sec
    if end - start < min_sec:                      # too short: stretch to the first sentence end that fits
        reach = [e for e in ends if start + min_sec <= e <= start + max_sec and (duration <= 0 or e <= duration)]
        if reach:
            end = min(reach)
        elif duration <= 0 or start + min_sec <= duration:
            end = start + min_sec
        else:
            return None
    if end - start < min_sec - 0.05:
        return None

    return {
        "start": round(start, 2),
        "end": round(end, 2),
        "duration": round(end - start, 2),
        "score": _clean_score(item.get("score")),
        "title": _clean_text(item.get("title"), MAX_TITLE_CHARS),
        "hook": _clean_text(item.get("hook"), MAX_TITLE_CHARS),
        "reason": _clean_text(item.get("reason"), MAX_CAPTION_CHARS),
        "caption": _clean_text(item.get("caption"), MAX_CAPTION_CHARS),
        "hashtags": _clean_hashtags(item.get("hashtags")),
        "source": str(item.get("source") or "model"),
    }


def _overlap_ratio(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    shared = min(a["end"], b["end"]) - max(a["start"], b["start"])
    shorter = min(a["end"] - a["start"], b["end"] - b["start"])
    return max(0.0, shared) / shorter if shorter > 0 else 1.0


def choose_clips(candidates: Sequence[Dict[str, Any]], count: int, max_overlap: float = MAX_OVERLAP) -> List[Dict[str, Any]]:
    """
    The best ``count`` candidates that do not overlap each other (best score first, earlier wins a tie),
    returned in time order with ``rank`` (1 = best) and ``index`` (position in the video, from 1).
    """
    ordered = sorted(candidates, key=lambda c: (-c["score"], c["start"]))
    picked: List[Dict[str, Any]] = []
    for candidate in ordered:
        if len(picked) >= max(0, int(count)):
            break
        if all(_overlap_ratio(candidate, other) <= max_overlap for other in picked):
            picked.append(dict(candidate, rank=len(picked) + 1))
    picked.sort(key=lambda c: c["start"])
    for position, clip in enumerate(picked, 1):
        clip["index"] = position
    return picked


def top_up(chosen: Sequence[Dict[str, Any]], extra: Sequence[Dict[str, Any]], count: int,
           max_overlap: float = MAX_OVERLAP) -> List[Dict[str, Any]]:
    """
    ``chosen`` (model picks) plus as many of the best ``extra`` candidates (offline picks) that do not overlap
    anything already kept, up to ``count``; the model's picks are never replaced. Returned in time order.
    """
    kept = [dict(c) for c in chosen]
    for candidate in sorted(extra, key=lambda c: (-c["score"], c["start"])):
        if len(kept) >= max(0, int(count)):
            break
        if all(_overlap_ratio(candidate, other) <= max_overlap for other in kept):
            kept.append(dict(candidate, rank=len(kept) + 1))
    kept.sort(key=lambda c: c["start"])
    for position, clip in enumerate(kept, 1):
        clip["index"] = position
    return kept


# ── offline fallback ─────────────────────────────────────────────────────────
def _word_count(text: str) -> float:
    """Words for spaced scripts, plus a share of the characters for scripts written without spaces."""
    spaced = len(text.split())
    unspaced = len(re.findall(r"[぀-ヿ㐀-鿿가-힯က-႟฀-๿]", text))
    return max(spaced, unspaced * 0.45)


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith(_SENTENCE_END)


def _score_window(window: Sequence[Dict[str, Any]], focus: str) -> float:
    start, end = _start(window[0]), _end(window[-1])
    length = max(0.1, end - start)
    text = " ".join(text_of(s) for s in window)
    words = _word_count(text)
    lowered = text.lower()

    density = 1.0 - min(1.0, abs(words / length - 2.8) / 2.8)               # about 2.8 words/s is lively speech
    speech = min(1.0, (sum(_end(s) - _start(s) for s in window) / length) / 0.85)
    punctuation = min(1.0, (lowered.count("?") + lowered.count("!")) / 6.0)

    first = text_of(window[0]).lower()
    hook = 0.0
    if _word_count(first) <= 16:
        hook = 0.4
        if "?" in first or any(ch.isdigit() for ch in first) or any(w in first for w in _STRONG_WORDS):
            hook = 1.0

    if focus in _FOCUS_WORDS:
        hits = sum(lowered.count(w) for w in _FOCUS_WORDS[focus])
        focus_score = min(1.0, hits / 3.0)
    else:
        focus_score = 0.5
    return round(100.0 * (0.35 * density + 0.20 * speech + 0.15 * punctuation + 0.20 * hook + 0.10 * focus_score), 1)


def heuristic_candidates(segments: Sequence[Dict[str, Any]], min_sec: float, max_sec: float,
                         focus: str = "balanced") -> List[Dict[str, Any]]:
    """
    Clip candidates without a model: every window that starts at a sentence start and ends at a sentence end,
    scored on speech density, questions / exclamations, an eye-catching first line and the chosen focus.
    """
    segs = [s for s in segments if text_of(s)]
    windows: List[Dict[str, Any]] = []
    for i, first in enumerate(segs):
        if i > 0 and not (_start(first) - _end(segs[i - 1]) >= 0.5 or _ends_sentence(text_of(segs[i - 1]))):
            continue                                              # not the beginning of a sentence
        best: Optional[Tuple[float, int]] = None
        for j in range(i, len(segs)):
            length = _end(segs[j]) - _start(first)
            if length > max_sec:
                break
            closes = (_ends_sentence(text_of(segs[j])) or j == len(segs) - 1
                      or _start(segs[j + 1]) - _end(segs[j]) >= 0.6)
            if length >= min_sec and closes:
                score = _score_window(segs[i:j + 1], focus)
                if best is None or score > best[0]:
                    best = (score, j)
        if best is not None:
            score, j = best
            sentence = text_of(first)
            windows.append({
                "start": round(_start(first), 2),
                "end": round(_end(segs[j]), 2),
                "duration": round(_end(segs[j]) - _start(first), 2),
                "score": score,
                "title": _clean_text(sentence, 70),
                "hook": _clean_text(sentence, MAX_TITLE_CHARS),
                "reason": "Chosen without the AI model: lively speech and a strong opening.",
                "caption": "",
                "hashtags": [],
                "source": "heuristic",
            })
    return windows
