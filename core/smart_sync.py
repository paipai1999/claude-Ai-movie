"""
Smart Sync: the picture follows the voice
=========================================
The default way of fitting narration to a film (``sync_mode = audio_fit``) leaves the picture alone and stretches or
squeezes every voice clip into the time slot of its scene (0.78x - 1.15x). A line that is too long for its slot has to
be cut short by the QA step, so part of the story is lost; a line that is short leaves silence.

Smart Sync turns this around: the voice is spoken at its natural pace and the *picture* of each scene is re-timed to
the length of its line (slowed down by at most ``max_slowdown``, sped up by at most ``max_speedup``). Only when a line
is longer than the picture may be slowed, the voice itself is sped up (at most ``max_voice_speedup``) and, as the
last resort, the picture is slowed down further (reported as a warning).

This module is the arithmetic: it turns "scene (start, end) + length of its voice" into a list of segments that
covers the whole film and says, for each, how long it lasts on the re-timed video. Pure functions and plain data,
so every rule can be tested without FFmpeg. The rendering of the re-timed video is ``agents/merger/smart_sync.py``.

Smart Sync - ပုံကို အသံနှင့် ညှိသည်။ scene တစ်ခုချင်းစီ၏ ပုံကို အသံကြာချိန်နှင့် ကိုက်အောင် နှေး/မြန်စေသည်။
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

MODES = ("audio_fit", "smart")
DEFAULT_MODE = "audio_fit"

MIN_SLOT_SEC = 0.5            # a scene shorter than this is given this much picture to be slowed or sped up
WARN_SLOWDOWN = 1.6           # a picture slowed more than this is reported to the user


def normalize_mode(value) -> str:
    """"smart" / "Smart Sync" -> "smart"; anything else is the default, "audio_fit"."""
    text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return "smart" if text in ("smart", "smart_sync") else DEFAULT_MODE


@dataclass(frozen=True)
class SyncLimits:
    max_slowdown: float = 1.25        # the picture of a scene may last up to 1.25x its original length
    max_speedup: float = 1.25         # ... or be played up to 1.25x faster (lasting 1/1.25 of it)
    max_voice_speedup: float = 1.15   # a clip that still does not fit is sped up by at most this

    @property
    def lo(self) -> float:
        return 1.0 / max(1.0, self.max_speedup)

    @property
    def hi(self) -> float:
        return max(1.0, self.max_slowdown)


def limits_from_config(config: Optional[Dict[str, Any]]) -> SyncLimits:
    """``config.json`` -> ``sync.smart`` (any missing or silly value falls back to the default, within sane bounds)."""
    section = ((config or {}).get("sync") or {}).get("smart") or {}

    def number(key: str, default: float, low: float, high: float) -> float:
        try:
            return max(low, min(high, float(section.get(key, default))))
        except (TypeError, ValueError):
            return default

    return SyncLimits(
        max_slowdown=number("max_slowdown", 1.25, 1.0, 2.0),
        max_speedup=number("max_speedup", 1.25, 1.0, 2.0),
        max_voice_speedup=number("max_voice_speedup", 1.15, 1.0, 1.5),
    )


@dataclass(frozen=True)
class Segment:
    """A stretch of the source film and where it lands on the re-timed film. ``block`` is None for the film between lines."""
    src_start: float
    src_end: float
    out_start: float
    out_end: float
    block: Optional[int] = None

    @property
    def src_length(self) -> float:
        return self.src_end - self.src_start

    @property
    def out_length(self) -> float:
        return self.out_end - self.out_start

    @property
    def factor(self) -> float:
        """How much longer the picture lasts: 1.0 = unchanged, above 1 = slowed down, below 1 = sped up."""
        return self.out_length / self.src_length if self.src_length > 0 else 1.0


@dataclass
class SyncPlan:
    segments: List[Segment] = field(default_factory=list)
    voice_tempo: Dict[int, float] = field(default_factory=dict)       # block -> atempo for its clip (1.0 = natural)
    warnings: List[str] = field(default_factory=list)
    dropped: List[int] = field(default_factory=list)                  # blocks that start after the film ended
    check: Dict[str, Any] = field(default_factory=dict)               # the run-end check of the rendered film (see ``verify``)

    @property
    def out_duration(self) -> float:
        return self.segments[-1].out_end if self.segments else 0.0

    @property
    def src_duration(self) -> float:
        return self.segments[-1].src_end if self.segments else 0.0

    @property
    def is_identity(self) -> bool:
        return all(abs(s.factor - 1.0) < 1e-6 for s in self.segments)

    def block_segment(self, block: int) -> Optional[Segment]:
        for segment in self.segments:
            if segment.block == block:
                return segment
        return None

    def block_start(self, block: int) -> Optional[float]:
        """Where the voice of ``block`` starts on the re-timed film."""
        segment = self.block_segment(block)
        return segment.out_start if segment else None

    def to_output(self, src_time: float) -> float:
        """A time on the source film -> the same moment on the re-timed film (piecewise linear, clamped to the film)."""
        if not self.segments:
            return float(src_time)
        if src_time <= self.segments[0].src_start:
            return self.segments[0].out_start
        for segment in self.segments:
            if src_time <= segment.src_end:
                return segment.out_start + (src_time - segment.src_start) * segment.factor
        return self.segments[-1].out_end

    def to_source(self, out_time: float) -> float:
        """The reverse of ``to_output``."""
        if not self.segments:
            return float(out_time)
        for segment in self.segments:
            if out_time <= segment.out_end:
                return segment.src_start + max(0.0, out_time - segment.out_start) / segment.factor
        return self.segments[-1].src_end

    def summary(self) -> Dict[str, Any]:
        """The numbers worth showing: how many scenes were slowed / sped up / left alone, and the new length."""
        blocks = [s for s in self.segments if s.block is not None]
        return {
            "scenes": len(blocks),
            "slowed": sum(1 for s in blocks if s.factor > 1.02),
            "sped_up": sum(1 for s in blocks if s.factor < 0.98),
            "unchanged": sum(1 for s in blocks if 0.98 <= s.factor <= 1.02),
            "voice_sped_up": sum(1 for t in self.voice_tempo.values() if t > 1.005),
            "source_seconds": round(self.src_duration, 2),
            "output_seconds": round(self.out_duration, 2),
            "slowest": round(max((s.factor for s in blocks), default=1.0), 3),
            "fastest": round(min((s.factor for s in blocks), default=1.0), 3),
        }

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly form, stored with the project (``sync_plan.json``)."""
        return {
            "summary": self.summary(),
            "warnings": list(self.warnings),
            "segments": [[round(s.src_start, 3), round(s.src_end, 3), round(s.out_start, 3), round(s.out_end, 3), s.block]
                         for s in self.segments],
            "voice_tempo": {str(k): round(v, 4) for k, v in self.voice_tempo.items() if abs(v - 1.0) > 1e-3},
            "check": dict(self.check),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SyncPlan":
        segments = [Segment(float(a), float(b), float(c), float(d), None if e is None else int(e))
                    for a, b, c, d, e in (data or {}).get("segments", [])]
        tempo = {int(k): float(v) for k, v in ((data or {}).get("voice_tempo") or {}).items()}
        return cls(segments, tempo, list((data or {}).get("warnings") or []), check=dict((data or {}).get("check") or {}))


def _frame_quantized(seconds: float, fps: float) -> float:
    """A length rounded to a whole number of frames (at least one), so that picture and sound add up exactly."""
    if fps <= 0:
        return seconds
    return max(1.0, round(seconds * fps)) / fps


def plan(blocks: Sequence[Tuple[float, float, float]], video_duration: float, limits: Optional[SyncLimits] = None,
         fps: float = 0.0) -> SyncPlan:
    """
    Builds the segments of the re-timed film.

    ``blocks``  one ``(src_start, src_end, voice_seconds)`` per narrated line, in the order of the film
    ``video_duration``  seconds of the source film
    ``fps``  when given, every segment of the new film lasts a whole number of frames

    Lines may overlap or be out of order in the script: they are sorted, and a line never starts before the previous
    one ended. A line that starts after the film ended is dropped (listed in ``SyncPlan.dropped``).
    """
    limits = limits or SyncLimits()
    result = SyncPlan()
    duration = max(0.0, float(video_duration))
    order = sorted(range(len(blocks)), key=lambda i: (float(blocks[i][0]), i))

    cursor_src = 0.0
    cursor_out = 0.0

    def add(src_start: float, src_end: float, out_length: float, block: Optional[int]) -> None:
        nonlocal cursor_out
        result.segments.append(Segment(src_start, src_end, cursor_out, cursor_out + out_length, block))
        cursor_out += out_length

    for index in order:
        start, end, voice = float(blocks[index][0]), float(blocks[index][1]), max(0.0, float(blocks[index][2]))
        start = max(start, cursor_src)                                       # never before the previous line ended
        if duration and start >= duration - 1e-6:
            result.dropped.append(index)
            continue
        end = max(end, start + MIN_SLOT_SEC)                                 # a very short scene gets room to move
        if duration:
            end = min(end, duration)
            if end - start < 1e-3:
                result.dropped.append(index)
                continue
        if start - cursor_src >= (0.5 / fps if fps > 0 else 1e-3):          # the film between two lines: unchanged
            add(cursor_src, start, _frame_quantized(start - cursor_src, fps), None)
        else:
            start = cursor_src                                               # less than half a frame: absorbed by this scene
        slot = end - start

        tempo = 1.0
        if voice <= 0.0:
            length = slot                                                    # nothing to follow: leave the scene alone
        else:
            ratio = voice / slot
            if ratio > limits.hi:
                wanted = voice / (slot * limits.hi)                          # speed the voice up before slowing the picture more
                if wanted <= limits.max_voice_speedup:
                    tempo, length = wanted, slot * limits.hi
                else:
                    tempo = limits.max_voice_speedup
                    length = voice / tempo                                   # the last resort: slow the picture further
                    if length / slot > WARN_SLOWDOWN:
                        result.warnings.append(
                            f"Line {index + 1}: its voice is {voice:.1f}s for a {slot:.1f}s scene, so the picture is slowed "
                            f"down x{length / slot:.2f}. Shorten the line in the Script Editor for a more natural result.")
            elif ratio < limits.lo:
                length = slot * limits.lo                                    # sped up as far as allowed; the voice ends early
            else:
                length = voice                                               # the picture lasts exactly as long as the voice
        length = _frame_quantized(length, fps)
        add(start, end, length, index)
        if voice > 0.0:
            result.voice_tempo[index] = tempo
        cursor_src = end

    if duration and duration - cursor_src >= (0.5 / fps if fps > 0 else 1e-3):   # the tail of the film
        add(cursor_src, duration, _frame_quantized(duration - cursor_src, fps), None)
    return result


def verify(sync: SyncPlan, picture_seconds: float, narration_end: float) -> Dict[str, Any]:
    """
    The check at the end of the re-timing: the film that was rendered must be as long as the plan said (every segment
    a whole number of frames, so it is within a frame or two), and the last line of the narration must end inside it.
    ``ok`` is False when the picture drifted from the plan or the voice would be cut off by the end of the film.
    """
    plan_seconds = sync.out_duration
    tolerance = 0.3 + 0.001 * plan_seconds
    drift = float(picture_seconds) - plan_seconds
    return {
        "ok": abs(drift) <= tolerance and float(narration_end) <= float(picture_seconds) + 0.5,
        "picture_seconds": round(float(picture_seconds), 3),
        "plan_seconds": round(plan_seconds, 3),
        "drift_seconds": round(drift, 3),
        "narration_end": round(float(narration_end), 3),
        "tolerance": round(tolerance, 3),
    }


# ─────────────────────────────────────────────────────────────────────────────
# FFmpeg filter strings (pure string building; the rendering is agents/merger/smart_sync.py)
# ─────────────────────────────────────────────────────────────────────────────
def atempo_filter(tempo: float) -> str:
    """``atempo`` for any speed: FFmpeg's filter takes 0.5 - 2.0 per step, so a larger change is chained."""
    tempo = max(0.25, min(4.0, float(tempo)))
    if abs(tempo - 1.0) < 1e-4:
        return "anull"
    steps = []
    while tempo > 2.0:
        steps.append(2.0)
        tempo /= 2.0
    while tempo < 0.5:
        steps.append(0.5)
        tempo /= 0.5
    steps.append(tempo)
    return ",".join(f"atempo={step:.5f}" for step in steps)


def segment_graph(factor: float, fps: float, has_audio: bool) -> str:
    """
    The filter graph that re-times ONE segment (its input starts at the segment's first frame): the picture is slowed
    by ``factor`` (above 1) or sped up (below 1) at a constant frame rate, the sound follows with pitch kept.
    Output labels: ``[v]`` and, with sound, ``[a]``. The caller cuts both to the exact length of the segment.
    """
    factor = max(0.05, float(factor))
    video = (f"[0:v]setpts=(PTS-STARTPTS)*{factor:.6f},fps={fps:.4f},"
             "tpad=stop_mode=clone:stop_duration=2,format=yuv420p[v]")
    if not has_audio:
        return video
    audio = (f"[0:a]{atempo_filter(1.0 / factor)},aresample=48000,"
             "aformat=sample_fmts=s16:channel_layouts=stereo,apad[a]")
    return f"{video};{audio}"
