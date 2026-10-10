from brain.diagnostics import log_suppressed
import cv2
import os
import subprocess
import json
from brain.memory import MovieState

class VideoAgent:
    def __init__(self, movie_path: str):
        self.movie_path = movie_path

    def _get_duration_via_ffprobe(self) -> str:
        """Fallback: use ffprobe or ffmpeg to get accurate duration when cv2 FPS returns 0."""
        import shutil, re
        ffprobe_bin = shutil.which("ffprobe")
        if ffprobe_bin:
            try:
                result = subprocess.run(
                    [ffprobe_bin, "-v", "quiet", "-print_format", "json",
                     "-show_format", self.movie_path],
                    capture_output=True, text=True, timeout=15,
                    encoding="utf-8", errors="replace"
                )
                if result.returncode == 0:
                    info = json.loads(result.stdout)
                    duration_sec = float(info.get("format", {}).get("duration", 0))
                    if duration_sec > 0:
                        hours = int(duration_sec // 3600)
                        minutes = int((duration_sec % 3600) // 60)
                        seconds = int(duration_sec % 60)
                        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            except Exception as _exc:
                log_suppressed("agents.video_agent._get_duration_via_ffprobe", _exc)

        # Fallback to ffmpeg -i info parsing
        ffmpeg_bin = shutil.which("ffmpeg") or os.environ.get("IMAGEIO_FFMPEG_EXE")
        if not ffmpeg_bin:
            try:
                from imageio_ffmpeg import get_ffmpeg_exe
                ffmpeg_bin = get_ffmpeg_exe()
            except Exception:
                ffmpeg_bin = None

        if ffmpeg_bin:
            try:
                res = subprocess.run(
                    [ffmpeg_bin, "-i", self.movie_path],
                    capture_output=True, text=True, timeout=15,
                    encoding="utf-8", errors="replace"
                )
                m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", res.stderr)
                if m:
                    hours, minutes, seconds = int(m.group(1)), int(m.group(2)), int(float(m.group(3)))
                    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            except Exception as _exc:
                log_suppressed("agents.video_agent._get_duration_via_ffprobe", _exc)
        return None

    def analyze_metadata(self, state: MovieState) -> MovieState:
        print(f"[*] VideoAgent: Analyzing metadata for {self.movie_path}")
        if not os.path.exists(self.movie_path):
            raise FileNotFoundError(f"Movie file not found: {self.movie_path}")

        cap = cv2.VideoCapture(self.movie_path)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video file: {self.movie_path}")

        try:
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

            if fps > 0 and frame_count > 0:
                duration_sec = frame_count / fps
                hours = int(duration_sec // 3600)
                minutes = int((duration_sec % 3600) // 60)
                seconds = int(duration_sec % 60)
                state.duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            else:
                # Fix: FPS=0 common for MKV/broken headers — fallback to ffprobe
                print("[WARN] VideoAgent: cv2 returned FPS=0. Trying ffprobe for accurate duration...")
                state.duration = self._get_duration_via_ffprobe()
                if state.duration:
                    print(f"[OK] VideoAgent: ffprobe duration -> {state.duration}")
                else:
                    print("[WARN] VideoAgent: Could not determine duration. Downstream agents will use safe fallbacks.")

            state.fps = fps
            state.frames_count = frame_count
            state.resolution = f"{width}x{height}"
            state.file_path = self.movie_path
        finally:
            cap.release()

        print(f"[*] VideoAgent completed: FPS={state.fps:.2f}, Resolution={state.resolution}, Duration={state.duration}")
        return state
