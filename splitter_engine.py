"""
Splitter - cut a long video into parts
======================================
Takes a long video (a file or a link), and cuts it into parts of N minutes or into N equal parts:

    1. ingest      a link is downloaded into the project folder (a local file is used where it is);
    2. plan        evenly spaced cut points (core/split_plan.py);
    3. snap        each cut moves to the nearest pause in the audio, so a part does not end mid-sentence;
    4. cut         stream copy by default (fast, lossless, cuts on keyframes) or a re-encode for
                   frame-accurate cuts;
    5. report      parts.json + state.json, so the part list shows up under Projects.

Nothing here changes the picture or the sound of the video.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agents.downloader_agent import DownloaderAgent
from agents.video_merger_agent import _get_ffmpeg_bin, _get_safe_ascii_id, detect_hardware_encoder
from brain.diagnostics import log_suppressed
from core import split_plan
from core.encoders import CPU_VIDEO_ARGS, is_cpu_encoder, video_encoder_args
from core.engine_base import StudioEngineBase
from core.paths import resolve_inside, sanitize_project_name

_MP4_LIKE = (".mp4", ".m4v", ".mov")
_KEEP_EXTENSIONS = _MP4_LIKE + (".mkv", ".webm")
_PART_NAME = re.compile(r"^part_\d+\.[A-Za-z0-9]+$")
_SILENCE_FILTER = "silencedetect=noise=-32dB:d=0.5"


def _clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


class SplitterEngine(StudioEngineBase):
    """Cuts one long video into parts. Shares init, cancellation and probing with the studio engines."""

    cancel_message = "SplitterEngine execution cancelled by user."
    announce_cancel = True

    def _resolve_ffmpeg_bin(self) -> str:
        return _get_ffmpeg_bin()

    # ── 1. ingest ────────────────────────────────────────────────────────────
    def _ingest(self, input_source: str, project_dir: str, resolution: str) -> Tuple[str, str, Optional[str]]:
        """(video path, title, temporary download folder to delete after a successful cut)."""
        self._check_cancellation()
        if self.is_url(input_source):
            print(f"[*] Input is a link -> downloading {input_source}")
            download_dir = os.path.join(project_dir, "_download")
            os.makedirs(download_dir, exist_ok=True)
            path = DownloaderAgent(output_dir=download_dir).download_video(input_source, resolution=resolution)
            return path, os.path.splitext(os.path.basename(path))[0], download_dir
        local = os.path.abspath(input_source)
        if not os.path.isfile(local):
            raise FileNotFoundError(f"Input video file not found: {local}")
        print(f"[*] Input is a local video -> {local}")
        return local, os.path.splitext(os.path.basename(local))[0], None

    # ── 3. pauses ────────────────────────────────────────────────────────────
    def _detect_pauses(self, video_path: str, duration: float) -> List[Tuple[float, float]]:
        """Pauses in the audio (audio only, so this is much faster than playing the video)."""
        self._check_cancellation()
        cmd = [self.ffmpeg_bin, "-hide_banner", "-nostats", "-i", video_path, "-vn", "-sn",
               "-af", _SILENCE_FILTER, "-f", "null", "-"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=max(900, int(duration * 0.6)))
        except Exception as exc:
            log_suppressed("splitter_engine._detect_pauses", exc)
            return []
        if res.returncode != 0:
            return []
        return split_plan.parse_silencedetect(res.stderr or "", duration)

    # ── 4. cutting ───────────────────────────────────────────────────────────
    @staticmethod
    def _clear_old_parts(project_dir: str) -> None:
        """A re-run into the same folder must not leave parts of the previous run behind."""
        for name in os.listdir(project_dir):
            if _PART_NAME.match(name):
                try:
                    os.remove(os.path.join(project_dir, name))
                except OSError as exc:
                    log_suppressed("splitter_engine._clear_old_parts", exc)

    def _cut_copy(self, video_path: str, cuts: List[float], project_dir: str, ext: str, duration: float) -> List[str]:
        """Stream-copy cut with FFmpeg's segment muxer: contiguous parts, each starting on a keyframe."""
        width = 3 if len(cuts) + 1 > 99 else 2
        pattern = os.path.join(project_dir, f"part_%0{width}d{ext}")
        cmd = [self.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-i", video_path,
               "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-f", "segment",
               "-segment_times", ",".join(f"{c:.3f}" for c in cuts),
               "-segment_start_number", "1", "-reset_timestamps", "1"]
        if ext in _MP4_LIKE:
            cmd += ["-segment_format_options", "movflags=+faststart"]
        cmd.append(pattern)
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=max(1800, int(duration)))
        parts = sorted(n for n in os.listdir(project_dir) if _PART_NAME.match(n))
        if res.returncode != 0 or not parts:
            tail = "\n".join(line for line in (res.stderr or "").splitlines() if line.strip())[-400:]
            raise RuntimeError(f"FFmpeg could not cut the video: {tail or 'no output'}")
        return [os.path.join(project_dir, n) for n in parts]

    def _cut_reencode(self, video_path: str, spans: List[Tuple[float, float]], project_dir: str, duration: float) -> List[str]:
        """Frame-accurate cut: every part is encoded on its own (hardware encoder first, CPU as the fallback)."""
        width = 3 if len(spans) > 99 else 2
        encoder = video_encoder_args(detect_hardware_encoder())
        outputs: List[str] = []
        for number, (start, end) in enumerate(spans, 1):
            self._check_cancellation()
            target = os.path.join(project_dir, f"part_{number:0{width}d}.mp4")
            print(f"[*] Encoding part {number}/{len(spans)} ({_clock(start)} - {_clock(end)})...")
            attempts = [encoder] if is_cpu_encoder(encoder) else [encoder, CPU_VIDEO_ARGS]
            for attempt, video_args in enumerate(attempts, 1):
                cmd = [self.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}",
                       "-i", video_path, "-t", f"{end - start:.3f}", "-map", "0:v:0", "-map", "0:a?",
                       *video_args, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                       "-movflags", "+faststart", target]
                res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                     timeout=max(3600, int((end - start) * 6)))
                if res.returncode == 0 and os.path.exists(target) and os.path.getsize(target) > 1000:
                    outputs.append(target)
                    break
                self._check_cancellation()                  # a killed FFmpeg (Stop button) must not trigger a retry
                if attempt == len(attempts):
                    raise RuntimeError(f"FFmpeg could not encode part {number}: {(res.stderr or '')[-300:]}")
                print(f"[WARN] Hardware encoder failed on part {number}; retrying on the CPU.")
        return outputs

    # ── run ──────────────────────────────────────────────────────────────────
    def run(
        self,
        input_source: str,
        project_name: Optional[str] = None,
        split_mode: str = "duration",
        split_minutes: float = 10.0,
        split_parts: int = 2,
        split_snap: bool = True,
        split_reencode: bool = False,
        resolution: str = "1080p",
    ) -> Dict[str, Any]:
        started = time.time()
        safe_id = _get_safe_ascii_id(project_name or input_source, prefix="proj")
        clean_name = sanitize_project_name(project_name, fallback=f"split_{safe_id}")
        project_dir = resolve_inside(self.output_base_dir, clean_name)
        if not project_dir:
            raise ValueError(f"Invalid project name: {project_name!r}")
        os.makedirs(project_dir, exist_ok=True)

        print(f"\nSplitterEngine: Initializing '{clean_name}'...")
        print(f"   Mode: {split_mode} | Every: {split_minutes} min | Parts: {split_parts} | "
              f"Snap to pauses: {split_snap} | Frame-accurate: {split_reencode}")

        print("\n--- [Phase 1: Ingesting the video] ---", flush=True)
        video_path, title, download_dir = self._ingest(input_source, project_dir, resolution)
        meta = self._probe_video_metadata(video_path)
        duration = float(meta.get("duration") or 0.0)
        print(f"[OK] '{title}' is {_clock(duration)} long.")

        print("\n--- [Phase 2: Planning the cuts] ---", flush=True)
        cuts = split_plan.ideal_cuts(duration, split_mode, minutes=split_minutes, parts=split_parts)
        if not cuts:
            raise ValueError(
                f"The video ({_clock(duration)}) is not longer than one part ({split_minutes:g} min), so there is "
                "nothing to split. Use a shorter part length.")
        snapped = False
        if split_snap and meta.get("has_audio", True):
            pauses = self._detect_pauses(video_path, duration)
            moved = split_plan.snap_cuts(cuts, pauses, duration)
            snapped = moved != cuts
            print(f"[OK] Found {len(pauses)} pause(s); {sum(1 for a, b in zip(cuts, moved) if a != b)} of "
                  f"{len(cuts)} cut(s) moved onto a pause.")
            cuts = moved
        spans = split_plan.parts_from_cuts(duration, cuts)
        print(f"[*] {len(spans)} parts planned.")

        print("\n--- [Phase 3: Cutting] ---", flush=True)
        self._check_cancellation()
        self._clear_old_parts(project_dir)
        extension = os.path.splitext(video_path)[1].lower()
        extension = extension if extension in _KEEP_EXTENSIONS else ".mp4"
        if split_reencode:
            files = self._cut_reencode(video_path, spans, project_dir, duration)
            extension = ".mp4"
        else:
            files = self._cut_copy(video_path, cuts, project_dir, extension, duration)

        parts: List[Dict[str, Any]] = []
        cursor = 0.0
        for number, path in enumerate(files, 1):
            length = float(self._probe_video_metadata(path).get("duration") or 0.0)
            parts.append({
                "index": number,
                "file": os.path.basename(path),
                "start": round(cursor, 2),
                "end": round(cursor + length, 2),
                "duration": round(length, 2),
                "size_mb": round(os.path.getsize(path) / 1e6, 1),
            })
            cursor += length
        if len(parts) != len(spans):
            print(f"[WARN] {len(spans)} parts were planned but {len(parts)} were written (the video has few keyframes).")

        if download_dir:
            shutil.rmtree(download_dir, ignore_errors=True)

        self._write_reports(project_dir, title, duration, parts, split_mode, split_minutes, split_parts, snapped, split_reencode)
        elapsed = time.time() - started
        print("\n" + "=" * 65)
        print(f"SplitterEngine COMPLETED in {elapsed:.1f}s: {len(parts)} parts in {project_dir}")
        print("=" * 65 + "\n")
        return {
            "status": "completed",
            "project_dir": project_dir,
            "parts": parts,
            "duration_sec": elapsed,
            "total_records": len(parts),
            "phase": f"Split into {len(parts)} parts",
        }

    # ── 5. report ────────────────────────────────────────────────────────────
    def _write_reports(self, project_dir: str, title: str, duration: float, parts: List[Dict[str, Any]],
                       mode: str, minutes: float, count: int, snapped: bool, reencoded: bool) -> None:
        settings = {"mode": mode, "minutes": minutes, "parts": count, "snapped_to_pauses": snapped,
                    "frame_accurate": reencoded}
        with open(os.path.join(project_dir, "parts.json"), "w", encoding="utf-8") as fh:
            json.dump({"source": title, "duration": round(duration, 2), "settings": settings, "parts": parts},
                      fh, ensure_ascii=False, indent=2)
        state = {
            "movie_name": title,
            "engine_type": "splitter",
            "pipeline_status": "COMPLETED",
            "progress": 100,
            "total_records": len(parts),
            "duration": duration,
            "total_duration_formatted": _clock(duration),
            "updated_at": datetime.datetime.now().isoformat(),
        }
        with open(os.path.join(project_dir, "state.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        try:
            from brain.sqlite_store import save_custom_movie_state
            save_custom_movie_state(
                project_dir=os.path.basename(os.path.normpath(project_dir)), movie_name=title, movie_path="",
                language="-", whisper_model="-", progress=100, current_phase="Completed",
                state_dict=state, output_dir=self.output_base_dir,
            )
        except Exception as exc:
            log_suppressed("splitter_engine._write_reports", exc)


def main():
    from core.cli import SPLITTER, add_option_arguments, resolve_args

    parser = argparse.ArgumentParser(description="Splitter - cut a long video into parts")
    parser.add_argument("input", help="Video path or URL")
    parser.add_argument("--cookies", default=None, help="Path to cookies.txt file for YouTube download")
    add_option_arguments(parser, SPLITTER)
    args = parser.parse_args()
    v = resolve_args(SPLITTER, args)

    engine = SplitterEngine(cookies_path=args.cookies)
    result = engine.run(
        input_source=args.input,
        project_name=v["project_name"],
        split_mode=v["split_mode"],
        split_minutes=v["split_minutes"],
        split_parts=v["split_parts"],
        split_snap=v["split_snap"],
        split_reencode=v["split_reencode"],
        resolution=v["resolution"],
    )
    if isinstance(result, dict) and result.get("status") == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
