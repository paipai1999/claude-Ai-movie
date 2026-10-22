"""
Splitter planning - where to cut a long video
=============================================
Pure functions (no FFmpeg, no files) so the rules can be tested exhaustively:

* ``ideal_cuts``   the evenly spaced cut points for "every N minutes" or "N equal parts";
* ``parse_silencedetect`` turns FFmpeg's ``silencedetect`` log into (start, end) pauses;
* ``snap_cuts``    moves each cut to the nearest pause so a part does not end in the middle of a sentence;
* ``parts_from_cuts`` the (start, end) of every resulting part.

The splitter cuts with stream copy, which can only start a part on a keyframe, so these times are the
*intended* cut points; the real part lengths are measured after cutting.
"""

import re
from typing import List, Sequence, Tuple

MIN_PART_SEC = 20.0          # no part shorter than this (a cut closer to its neighbour is dropped)
SILENCE_MIN_SEC = 0.35       # a pause shorter than this is just a breath, not a place to cut
TAIL_FRACTION = 0.25         # a leftover shorter than this share of a part joins the last part

MODE_DURATION = "duration"   # every N minutes
MODE_PARTS = "parts"         # N equal parts
MODES = (MODE_DURATION, MODE_PARTS)

_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def ideal_cuts(duration: float, mode: str, minutes: float = 10.0, parts: int = 2) -> List[float]:
    """The interior cut points (seconds) before any snapping; empty when the video fits in one part."""
    if duration <= 0:
        raise ValueError("The video length is unknown, so it cannot be split.")
    if mode == MODE_PARTS:
        count = int(parts)
        if count < 2:
            raise ValueError("Split into at least 2 parts.")
        if duration / count < MIN_PART_SEC:
            raise ValueError(f"{count} parts would be shorter than {MIN_PART_SEC:.0f} seconds each.")
        return [duration * i / count for i in range(1, count)]
    if mode != MODE_DURATION:
        raise ValueError(f"Unknown split mode {mode!r}; use one of {', '.join(MODES)}.")

    step = float(minutes) * 60.0
    if step < MIN_PART_SEC:
        raise ValueError(f"A part must be at least {MIN_PART_SEC:.0f} seconds long.")
    cuts: List[float] = []
    point = step
    # A short leftover joins the previous part instead of becoming a tiny last file.
    while point < duration - step * TAIL_FRACTION:
        cuts.append(point)
        point += step
    return cuts


def parse_silencedetect(log: str, duration: float = 0.0) -> List[Tuple[float, float]]:
    """(start, end) of every pause in an FFmpeg ``silencedetect`` log; a pause open at EOF ends at ``duration``."""
    pauses: List[Tuple[float, float]] = []
    start = None
    for line in log.splitlines():
        found = _SILENCE_START.search(line)
        if found:
            start = max(0.0, float(found.group(1)))
            continue
        found = _SILENCE_END.search(line)
        if found and start is not None:
            end = float(found.group(1))
            if end > start:
                pauses.append((start, end))
            start = None
    if start is not None and duration > start:
        pauses.append((start, duration))
    return pauses


def snap_cuts(cuts: Sequence[float], pauses: Sequence[Tuple[float, float]], duration: float,
              window: float = 20.0) -> List[float]:
    """
    Moves every cut to the middle of the nearest pause within ``window`` seconds (the longer pause wins a tie).
    A cut stays where it was when snapping would leave a part shorter than ``MIN_PART_SEC`` or reorder cuts.
    """
    cuts = [float(c) for c in cuts]
    if not cuts:
        return []
    candidates = [((s + e) / 2.0, e - s) for s, e in pauses if e - s >= SILENCE_MIN_SEC]
    if not candidates:
        return cuts

    # Never reach across half a part: two cuts must not snap to the same pause.
    gaps = [b - a for a, b in zip([0.0] + cuts, cuts + [duration])]
    window = min(window, 0.4 * min(gaps)) if gaps else window

    result: List[float] = []
    previous = 0.0
    for index, cut in enumerate(cuts):
        near = [(abs(mid - cut), -length, mid) for mid, length in candidates if abs(mid - cut) <= window]
        chosen = min(near)[2] if near else cut
        upcoming = cuts[index + 1] if index + 1 < len(cuts) else duration
        if chosen - previous < MIN_PART_SEC or upcoming - chosen < MIN_PART_SEC:
            chosen = cut
        result.append(round(chosen, 3))
        previous = chosen
    return result


def parts_from_cuts(duration: float, cuts: Sequence[float]) -> List[Tuple[float, float]]:
    """(start, end) of every part for the given interior cuts."""
    edges = [0.0] + [float(c) for c in cuts] + [float(duration)]
    return [(a, b) for a, b in zip(edges, edges[1:])]
