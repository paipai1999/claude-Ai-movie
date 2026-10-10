"""
Media Probing & Audio Assembly Helpers (Media အချက်အလက် စစ်ဆေးခြင်းနှင့် အသံပေါင်းစပ်ခြင်း)
========================================================================================
Small FFmpeg-backed utilities shared by the merger, reels and QA code: ASCII-safe temp ids,
audio/video duration probing, audio-stream detection and the linear voiceover track assembler.

ဤ module သည် ဗီဒီယို/အသံ အရှည်စစ်ဆေးခြင်း၊ အသံပါ/မပါ စစ်ဆေးခြင်းနှင့် voiceover track တည်ဆောက်ခြင်း
helper များကို စုစည်းထားသည်။
"""

from brain.diagnostics import log_suppressed
import os
import re
import subprocess

from agents.merger.encoder import _get_ffmpeg_bin


def _get_safe_ascii_id(val: str, prefix: str = "vid", max_len: int = 16) -> str:
    """Generates an ASCII-safe unique identifier for temp files and FFmpeg filters,
    preventing libass fopen failures on Windows when video names contain Unicode/Burmese."""
    import hashlib
    raw = str(val or "").strip()
    stem = os.path.splitext(os.path.basename(raw))[0] if ("/" in raw or "\\" in raw or "." in raw) else raw
    ascii_clean = re.sub(r'[^a-zA-Z0-9_\-]', '_', stem).strip('_')
    h = hashlib.md5(raw.encode('utf-8', errors='replace')).hexdigest()[:8]
    if ascii_clean:
        return f"{ascii_clean[:max_len]}_{h}"
    return f"{prefix}_{h}"

def _get_audio_duration(file_path: str) -> float:
    """Fast sub-millisecond audio duration check using soundfile, wave, or ffprobe (bypasses MoviePy)."""
    if not file_path or not os.path.exists(file_path):
        return 0.0
    try:
        import soundfile as sf
        return float(sf.info(file_path).duration)
    except Exception as _exc:
        log_suppressed("agents.merger.media_utils._get_audio_duration", _exc)
    if file_path.lower().endswith('.wav'):
        try:
            import wave
            with wave.open(file_path, 'rb') as wf:
                return float(wf.getnframes()) / float(wf.getframerate())
        except Exception as _exc:
            log_suppressed("agents.merger.media_utils._get_audio_duration", _exc)
    try:
        ffmpeg_bin = _get_ffmpeg_bin()
        res = subprocess.run(
            [ffmpeg_bin, "-i", file_path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace"
        )
        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", res.stderr or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception as _exc:
        log_suppressed("agents.merger.media_utils._get_audio_duration", _exc)
    return 0.0

def _get_video_info(video_path: str) -> dict:
    """Fast video metadata extraction using OpenCV or FFprobe in 10ms without MoviePy."""
    info = {"duration": 0.0, "width": 1920, "height": 1080, "fps": 24.0}
    if not video_path or not os.path.exists(video_path):
        return info
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        if cap.isOpened():
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0)
            fc = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
            dur = fc / fps if fps > 0 else 0.0
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920)
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080)
            cap.release()
            info["duration"] = max(0.0, dur)
            info["width"] = w
            info["height"] = h
            info["fps"] = fps
            return info
    except Exception as _exc:
        log_suppressed("agents.merger.media_utils._get_video_info", _exc)
    try:
        ffmpeg_bin = _get_ffmpeg_bin()
        res = subprocess.run(
            [ffmpeg_bin, "-i", video_path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace"
        )
        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", res.stderr or "")
        if m:
            info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        dim = re.search(r",\s*(\d{3,4})x(\d{3,4})", res.stderr or "")
        if dim:
            info["width"] = int(dim.group(1))
            info["height"] = int(dim.group(2))
    except Exception as _exc:
        log_suppressed("agents.merger.media_utils._get_video_info", _exc)
    return info

def _has_audio_stream(video_path: str) -> bool:
    """Checks if a video file contains an audio stream using FFmpeg."""
    if not video_path or not os.path.exists(video_path):
        return False
    try:
        ffmpeg_bin = _get_ffmpeg_bin()
        res = subprocess.run(
            [ffmpeg_bin, "-i", video_path],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace"
        )
        return "Audio:" in (res.stderr or "")
    except Exception:
        return False

def _assemble_voiceover_track(clips_with_timing: list, total_duration: float, output_path: str, target_sr: int = 44100) -> str:
    """Stitches discrete speech clips into a contiguous PCM WAV buffer at C-speed in ~2 seconds."""
    import math
    import numpy as np
    total_samples = max(int(math.ceil(total_duration * target_sr)), target_sr)
    vo_buffer = np.zeros(total_samples, dtype=np.float32)

    for fpath, place_time, dur in clips_with_timing:
        if not fpath or not os.path.exists(fpath):
            continue
        try:
            import soundfile as sf
            data, c_sr = sf.read(fpath)
        except Exception:
            continue

        if data.ndim > 1:
            data = np.mean(data, axis=1)

        if c_sr != target_sr and len(data) > 0:
            try:
                import scipy.signal
                num_target = int(round(len(data) * float(target_sr) / float(c_sr)))
                data = scipy.signal.resample(data, num_target).astype(np.float32)
            except Exception as _exc:
                log_suppressed("agents.merger.media_utils._assemble_voiceover_track", _exc)

        start_idx = int(place_time * target_sr)
        end_idx = min(start_idx + len(data), total_samples)
        if start_idx < total_samples and end_idx > start_idx:
            # Overlap-add mixing: blend overlapping speech tails cleanly rather than clipping/overwriting
            vo_buffer[start_idx:end_idx] += data[:end_idx - start_idx]

    # Prevent 16-bit integer clipping distortion if overlapping signals exceed unity
    max_val = float(np.max(np.abs(vo_buffer))) if len(vo_buffer) > 0 else 0.0
    if max_val > 1.0:
        vo_buffer = vo_buffer / max_val

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    import soundfile as sf
    sf.write(output_path, vo_buffer, target_sr, subtype='PCM_16')
    return output_path
