"""
Speech-to-text with Faster-Whisper
==================================
Extract a 16 kHz mono WAV, transcribe it, and return ``[{"start", "end", "text"}, ...]`` whose
end times never outlive the speech (see ``core.segment_timing``). Engines 2 and 3 used to carry
two near-identical copies.
"""

import os
import subprocess
import time
from typing import Callable, Dict, List, Optional

from brain.diagnostics import log_suppressed
from core.segment_timing import tighten_end
from core.whisper_settings import load_faster_whisper, resolve_beam_size

# A real 16 kHz WAV is far larger; anything below is an empty / header-only extraction.
_MIN_WAV_BYTES = 100


def transcribe_media(
    ffmpeg_bin: str,
    media_path: str,
    language: Optional[str],
    pipeline_cfg: Dict,
    *,
    temp_prefix: str,
    vad_parameters: Optional[Dict] = None,
    use_media_when_extraction_fails: bool = False,
    log: Callable[[str], None] = print,
) -> List[Dict]:
    """
    ``language``: ISO code, or ``"auto"`` / ``None`` to detect.
    ``use_media_when_extraction_fails``: hand the media file straight to Whisper when ffmpeg could
    not produce a WAV (Engine 3); otherwise an empty list is returned (Engine 2).
    """
    temp_audio = os.path.abspath(
        os.path.join("temp", f"{temp_prefix}_{os.getpid()}_{int(time.time() * 1000)}.wav")
    )
    os.makedirs(os.path.dirname(temp_audio), exist_ok=True)
    extracted = False
    try:
        cmd = [ffmpeg_bin, "-y", "-i", media_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", temp_audio]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0 and os.path.exists(temp_audio) and os.path.getsize(temp_audio) > _MIN_WAV_BYTES:
            extracted = True
            audio_target = temp_audio
        elif use_media_when_extraction_fails:
            log("[WARN] Audio extraction produced empty file or failed. Attempting direct file inspection...")
            audio_target = media_path
        else:
            log(f"[WARN] Audio extraction failed (video may have no audio track): {media_path}")
            return []

        model, model_size, device = load_faster_whisper(pipeline_cfg.get("whisper_model", "auto"))
        log(f"[*] Faster-Whisper ready: model={model_size}, device={device}")

        lang = None if language in ("auto", "", None) else language
        kwargs = {
            "language": lang,
            "beam_size": resolve_beam_size(pipeline_cfg),
            "vad_filter": True,
            "word_timestamps": True,
        }
        if vad_parameters:
            kwargs["vad_parameters"] = vad_parameters
        segments, _info = model.transcribe(audio_target, **kwargs)

        results: List[Dict] = []
        for seg in segments:
            text = seg.text.strip()
            if not text or seg.end <= seg.start:
                continue
            # Whisper segments run on through silence/music; never outlive the speech.
            end = tighten_end(seg.start, seg.end, text, getattr(seg, "words", None))
            results.append({"start": seg.start, "end": end, "text": text})
        return results
    finally:
        if extracted and os.path.exists(temp_audio):
            try:
                os.remove(temp_audio)
            except Exception as exc:
                log_suppressed("core.transcribe.transcribe_media", exc)
