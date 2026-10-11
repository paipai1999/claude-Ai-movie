"""
Burnt-in subtitle detection: frame selection and decision logic
================================================================
The vision model answers one frame at a time and is noisy: it misses subtitles that are on
screen and occasionally reports text that is not a subtitle. Blurring is destructive, so the
decision must not hang on a single answer.

* ``pick_sample_times`` looks where somebody is *speaking* - that is where a subtitle can be
  visible - and spreads the samples over the whole video.
* ``decide_subtitle_region`` takes a vote over every frame that could be analysed and only
  reports subtitles when enough of them agree, then derives a robust region from the
  agreeing frames.

Both functions are pure so they can be tested without a video or a model.
"""

import statistics
from typing import Dict, Iterable, List, Optional

DEFAULT_SAMPLE_COUNT = 7
MIN_POSITIVE_RATIO = 0.4   # share of analysed frames that must show subtitles
MIN_POSITIVES = 2          # ... and at least this many (when enough frames were analysed)
FEW_FRAMES = 4             # below this many analysed frames a single positive may suffice
SUBTITLE_MIN_Y = 0.5       # dialogue subtitles live in the lower half of the frame

_FALLBACK_FRACTIONS = (0.10, 0.25, 0.40, 0.55, 0.70, 0.85, 0.95)


def pick_sample_times(
    dialogue_starts: Optional[Iterable[float]],
    duration: float,
    count: int = DEFAULT_SAMPLE_COUNT,
    offset: float = 0.5,
) -> List[float]:
    """
    Returns up to ``count`` timestamps (seconds), ascending.

    Uses moments just after dialogue starts when known, evenly spread across the video;
    otherwise falls back to fixed fractions of the duration.
    """
    duration = float(duration or 0.0)
    latest = max(duration - 0.5, 0.0) if duration > 0 else None

    starts = sorted({round(float(t), 2) for t in (dialogue_starts or []) if t is not None and float(t) > 1.0})
    if starts:
        if len(starts) > count:
            step = (len(starts) - 1) / float(count - 1) if count > 1 else 0.0
            starts = [starts[int(round(i * step))] for i in range(count)]
        times = [t + offset for t in starts]
    elif duration > 0:
        times = [duration * f for f in _FALLBACK_FRACTIONS[:count]]
    else:
        return []

    if latest is not None:
        times = [min(t, latest) for t in times]
    return sorted({round(t, 2) for t in times})


def _normalise(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    # Some answers come back in pixels of a 1080p frame.
    return v / 1080.0 if v > 1.0 else v


def _upper_quartile(values: List[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.75 * (len(ordered) - 1))))]


def decide_subtitle_region(
    results: List[Dict],
    default_y: float = 0.82,
    default_h: float = 0.18,
) -> Dict:
    """
    ``results``: one parsed model answer per *successfully analysed* frame
    (``has_subtitles``, ``start_y_pct``, ``height_pct``).

    Returns ``{"found", "start_y", "height", "positives", "analysed", "reason"}``.
    """
    analysed = len(results)
    positives = []
    for r in results:
        if not isinstance(r, dict) or not r.get("has_subtitles", False):
            continue
        y = _normalise(r.get("start_y_pct", r.get("y")), default_y)
        h = _normalise(r.get("height_pct", r.get("h")), default_h)
        y = max(0.0, min(0.98, y))
        h = max(0.02, min(0.40, h))
        if y >= SUBTITLE_MIN_Y:
            positives.append((y, h))

    out = {
        "found": False,
        "start_y": default_y,
        "height": default_h,
        "positives": len(positives),
        "analysed": analysed,
        "reason": "",
    }

    if analysed == 0:
        out["reason"] = "no frame could be analysed"
        return out

    needed = MIN_POSITIVES if analysed >= FEW_FRAMES else 1
    ratio = len(positives) / float(analysed)
    if len(positives) < needed:
        out["reason"] = f"only {len(positives)}/{analysed} frames positive (need {needed})"
        return out
    if ratio < MIN_POSITIVE_RATIO:
        out["reason"] = f"only {len(positives)}/{analysed} frames positive (need {MIN_POSITIVE_RATIO:.0%})"
        return out

    ys = [p[0] for p in positives]
    hs = [p[1] for p in positives]
    start_y = max(SUBTITLE_MIN_Y, min(0.92, statistics.median(ys) - 0.02))
    # Upper quartile (not max): one oversized answer must not blur half the frame, while a
    # two-line subtitle that shows up in several frames still widens the region.
    height = max(0.05, min(1.0 - start_y, _upper_quartile(hs) + 0.04))

    out.update(found=True, start_y=start_y, height=height,
               reason=f"{len(positives)}/{analysed} frames positive")
    return out
