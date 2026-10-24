"""
Video encoder arguments for the tools
=====================================
``detect_hardware_encoder()`` (agents/merger/encoder.py) says which encoder this machine has; the
tools need it as FFmpeg arguments, with the same quality targets the studios use. Kept separate so the
splitter and the Shorts renderer share one mapping and one CPU fallback.
"""

from typing import Any, List, Mapping

# The CPU encoder every machine has; used when the hardware encoder fails or is missing.
CPU_VIDEO_ARGS: List[str] = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]

_QUALITY_ARGS = {
    "h264_qsv": ["-global_quality", "23"],
    "h264_nvenc": ["-cq", "22"],
    "h264_amf": ["-qp_p", "22", "-qp_i", "22"],
}


def video_encoder_args(info: Mapping[str, Any]) -> List[str]:
    """``-c:v <codec> -preset <preset> <quality>`` for the detected encoder (libx264 when nothing was detected)."""
    codec = info.get("codec") or "libx264"
    preset = info.get("preset") or "medium"
    quality = _QUALITY_ARGS.get(codec) or list(info.get("quality_args") or ["-crf", "21"])
    return ["-c:v", codec, "-preset", preset, *quality]


def is_cpu_encoder(args: List[str]) -> bool:
    """True when ``args`` already select libx264 (so a CPU retry would repeat the same attempt)."""
    return "libx264" in args
