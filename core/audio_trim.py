"""
Silence trimming for synthesized speech
=======================================
Edge TTS pads every clip with ~0.1-0.4 s of silence at the start and end. The voice-over is stretched
to fit a time slot, so that padding is stretched with it: speech starts late and the gaps between
lines grow. Trimming the dead air first makes the following tempo fit measure only real speech and
lets each line start exactly where it is placed.

``trim_silence`` never makes a clip worse: on any failure, or if the result would be (almost) empty,
the original file is left untouched.
"""

import os
import subprocess
from typing import Optional

from brain.diagnostics import log_suppressed

THRESHOLD_DB = -45        # below this level counts as silence (TTS noise floor is far lower)
KEEP_LEAD_SEC = 0.05      # a breath of lead-in so the first consonant is never clipped
KEEP_TAIL_SEC = 0.20      # natural decay / pause after the last word
MIN_KEEP_RATIO = 0.35     # refuse a trim that removes more than ~65 % of the clip (probably a bad detection)
MIN_RESULT_SEC = 0.30
_TIMEOUT_SEC = 30


def _duration(path: str) -> Optional[float]:
    try:
        import soundfile as sf
        return float(sf.info(path).duration)
    except Exception as exc:
        log_suppressed("core.audio_trim._duration", exc)
        return None


def _filter() -> str:
    """Trim the start, reverse, trim the (former) end the same way, reverse back."""
    one_end = f"silenceremove=start_periods=1:start_threshold={THRESHOLD_DB}dB:start_silence={{keep}}"
    return ",".join([one_end.format(keep=KEEP_LEAD_SEC), "areverse", one_end.format(keep=KEEP_TAIL_SEC), "areverse"])


def trim_silence(ffmpeg_bin: str, path: str) -> bool:
    """Trims leading/trailing silence of ``path`` in place; returns True when the file was shortened."""
    if not ffmpeg_bin or not os.path.exists(path):
        return False
    base, ext = os.path.splitext(path)
    tmp = f"{base}_trim{ext}"
    codec = ["-c:a", "libmp3lame", "-q:a", "2"] if ext.lower() == ".mp3" else []
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-i", path, "-af", _filter(), *codec, tmp],
            capture_output=True, timeout=_TIMEOUT_SEC,
        )
        if res.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 200:
            return False

        before, after = _duration(path), _duration(tmp)
        if before is not None and after is not None:
            if after < MIN_RESULT_SEC or after > before - 0.03 or after < before * MIN_KEEP_RATIO:
                return False   # nothing to gain, or a suspicious cut: keep the original
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log_suppressed("core.audio_trim.trim_silence", exc)
        return False
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
