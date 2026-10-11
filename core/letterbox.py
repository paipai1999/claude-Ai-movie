"""
CinemaScope & Letterbox Auto-Detection and Cropping Module
==========================================================
Detects hardcoded black bars (letterbox/pillarbox) in video sources,
such as 21:9 CinemaScope movies letterboxed into standard 16:9 containers.
Provides pixel-accurate crop coordinates to eliminate black bars during
9:16 vertical reels and standard canvas compositing.
"""

from brain.diagnostics import log_suppressed
import os
import re
import subprocess
from typing import Optional, Dict


def plan_canvas_mode(src_w: int, src_h: int, out_w: int, out_h: int, tolerance: float = 0.02) -> str:
    """
    Decides how a source frame is placed on the output canvas.

    Returns "direct" when the source already has the output aspect ratio (a plain scale is
    lossless in shape), otherwise "fit": the frame is scaled to fit *inside* the canvas, with a
    blurred copy of itself behind it. A plain scale of e.g. a 9:16 clip into 16:9 would squash it.
    Unknown source dimensions fall back to "direct" (the previous behaviour).
    """
    try:
        src_w, src_h, out_w, out_h = int(src_w), int(src_h), int(out_w), int(out_h)
    except (TypeError, ValueError):
        return "direct"
    if src_w <= 0 or src_h <= 0 or out_w <= 0 or out_h <= 0:
        return "direct"
    ratio = (src_w / src_h) / (out_w / out_h)
    return "direct" if abs(ratio - 1.0) <= tolerance else "fit"


def detect_letterbox_crop(
    video_path: str,
    ffmpeg_bin: str = "ffmpeg",
    sample_timestamps: Optional[list] = None,
    threshold_pct: float = 0.05,
) -> Optional[Dict[str, int]]:
    """
    Analyzes video sample frames using FFmpeg cropdetect to determine if the video
    contains consistent letterbox (top/bottom) or pillarbox (left/right) black bars.

    Parameters:
    - video_path: Path to the input video file.
    - ffmpeg_bin: Path to the FFmpeg executable.
    - sample_timestamps: List of timestamp seconds or fractional positions to sample.
    - threshold_pct: Minimum percentage of black bars to qualify as letterbox (default 5%).

    Returns:
    - Dict with 'w', 'h', 'x', 'y', 'crop_str' or None if video is full frame without black bars.
    """
    if not video_path or not os.path.exists(video_path):
        return None

    # Probe original video dimensions
    orig_w, orig_h = 0, 0
    duration_s = 0.0
    try:
        cmd_probe = [
            ffmpeg_bin, "-i", os.path.abspath(video_path)
        ]
        res_p = subprocess.run(cmd_probe, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        dur_m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", res_p.stderr)
        if dur_m:
            duration_s = int(dur_m.group(1)) * 3600 + int(dur_m.group(2)) * 60 + float(dur_m.group(3))
        res_m = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", res_p.stderr)
        if res_m:
            orig_w, orig_h = int(res_m.group(1)), int(res_m.group(2))
    except Exception as _exc:
        log_suppressed("core.letterbox.detect_letterbox_crop", _exc)

    if orig_w <= 0 or orig_h <= 0:
        return None

    # Determine probe sample timestamps (avoid intro logos and end credits)
    if not sample_timestamps:
        if duration_s > 60.0:
            sample_timestamps = [
                max(5.0, duration_s * 0.15),
                max(10.0, duration_s * 0.35),
                max(15.0, duration_s * 0.55),
            ]
        else:
            sample_timestamps = [1.0]

    detected_crops = []
    for ts in sample_timestamps:
        try:
            cmd = [
                ffmpeg_bin,
                "-ss", f"{ts:.2f}",
                "-i", os.path.abspath(video_path),
                "-vframes", "15",
                "-vf", "cropdetect=24:2:0",
                "-f", "null", "-",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            matches = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", res.stderr)
            if matches:
                # Use the last converged crop detection
                cw, ch, cx, cy = [int(v) for v in matches[-1]]
                detected_crops.append((cw, ch, cx, cy))
        except Exception:
            continue

    if not detected_crops:
        return None

    # Find the consensus crop (take median/min dimensions to ensure no black borders remain)
    ws = sorted([c[0] for c in detected_crops])
    hs = sorted([c[1] for c in detected_crops])
    xs = sorted([c[2] for c in detected_crops])
    ys = sorted([c[3] for c in detected_crops])

    # Median coordinates
    cw = ws[len(ws) // 2]
    ch = hs[len(hs) // 2]
    cx = xs[len(xs) // 2]
    cy = ys[len(ys) // 2]

    # Ensure even dimensions for YUV compatibility
    cw = (cw // 2) * 2
    ch = (ch // 2) * 2
    cx = (cx // 2) * 2
    cy = (cy // 2) * 2

    # Verify if black bars exceed threshold (e.g. CinemaScope height reduction >= 5%)
    h_reduction = (orig_h - ch) / float(orig_h)
    w_reduction = (orig_w - cw) / float(orig_w)

    is_letterboxed = (h_reduction >= threshold_pct) or (w_reduction >= threshold_pct)

    if not is_letterboxed:
        return None

    # Sanity bounds check
    if cw < 320 or ch < 180 or cw > orig_w or ch > orig_h:
        return None

    return {
        "w": cw,
        "h": ch,
        "x": cx,
        "y": cy,
        "orig_w": orig_w,
        "orig_h": orig_h,
        "crop_str": f"crop={cw}:{ch}:{cx}:{cy}",
        "h_reduction_pct": round(h_reduction * 100, 1),
        "w_reduction_pct": round(w_reduction * 100, 1),
    }
