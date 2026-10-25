"""
Smart Sync: re-timing the picture to the voice (ပုံကို အသံနှင့်ညှိခြင်း)
=====================================================================
Step 2b of the merge, only when the job asked for ``sync_mode = smart``. The arithmetic (which scene lasts how long)
is ``core/smart_sync.py``; this mixin does the work it asks for:

    1. make the plan from the voice clips and the scenes of the script;
    2. speed up the few voice clips that still do not fit (pitch kept);
    3. render a re-timed copy of the film: every segment is cut, slowed or sped up (picture and the film's own sound) and
       the segments are joined, so the rest of the merge (subtitles, blur, watermark, Reels) simply works on a film whose
       timeline already matches the voice;
    4. point the merge plan at the new film and at the new times of the lines. The script in the project is never
       changed: it keeps the times of the original film.

The segments are encoded one by one (a few seconds each), never in one huge filter graph: splitting a long film into
hundreds of branches makes FFmpeg hold the frames of all of them in memory.
"""

import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from agents.merger.encoder import _get_ffmpeg_bin, detect_hardware_encoder
from agents.merger.media_utils import _get_audio_duration, _get_video_info, _has_audio_stream
from agents.merger.plan import MergePlan
from brain.diagnostics import log_suppressed
from core import smart_sync
from core.encoders import CPU_VIDEO_ARGS, is_cpu_encoder, video_encoder_args

SYNC_PLAN_NAME = "sync_plan.json"
_MAX_FPS = 30.0                  # the single-pass render caps higher frame rates at 30 anyway


def _cancelled() -> bool:
    return os.environ.get("CURRENT_JOB_CANCELLED") == "1"


class SmartSyncMixin:
    """Mixin for VideoMergerAgent: fills ``plan.sync_plan`` and swaps in the re-timed film."""

    # ── the entry point ───────────────────────────────────────────────────────
    def _apply_smart_sync(self, plan: MergePlan, state, movie_path: str) -> str:
        """Returns the film to merge: a re-timed copy for ``smart``, the given one otherwise (or when nothing changes)."""
        if smart_sync.normalize_mode(getattr(state, "sync_mode", None)) != "smart":
            return movie_path
        items = plan.audio_items
        if not items or plan.video_dur <= 0:
            return movie_path
        if not (plan.script_blocks and isinstance(plan.script_blocks[0], dict) and "start_sec" in plan.script_blocks[0]):
            print("[WARN] SmartSync: the script has no scene times, so the picture cannot follow the voice. Using audio_fit.")
            self._warn(state, "Smart Sync was skipped: the script has no scene times.")
            return movie_path

        fps = plan.video_fps if 0 < plan.video_fps <= 32.0 else _MAX_FPS
        limits = smart_sync.limits_from_config(plan.config_data)
        blocks = []
        for _, _, duration, block in items:
            start = float(block.get("start_sec") or 0.0)
            blocks.append((start, float(block.get("end_sec") or (start + 3.0)), duration))
        sync = smart_sync.plan(blocks, plan.video_dur, limits, fps)
        for line in sync.warnings:
            print(f"[WARN] SmartSync: {line}")
            self._warn(state, line)
        if sync.is_identity and not any(t > 1.005 for t in sync.voice_tempo.values()):
            print("[*] SmartSync: every line already fits its scene; the picture is left as it is.")
            return movie_path

        summary = sync.summary()
        print(f"[*] SmartSync: {summary['scenes']} scene(s) follow their voice: {summary['slowed']} slowed, "
              f"{summary['sped_up']} sped up, {summary['voice_sped_up']} voice clip(s) sped up; "
              f"{summary['source_seconds']:.1f}s -> {summary['output_seconds']:.1f}s.")

        work_dir = self._sync_work_dir(plan)
        shutil.rmtree(work_dir, ignore_errors=True)
        os.makedirs(work_dir, exist_ok=True)
        plan.sync_work_dir = work_dir
        retimed = os.path.join(work_dir, "retimed.mkv")
        try:
            self._render_retimed_film(movie_path, sync, fps, retimed, work_dir, self._worker_count(plan))
            info = _get_video_info(retimed)
            plan.video_dur, plan.video_w, plan.video_h, plan.video_fps = info["duration"], info["width"], info["height"], info["fps"]
            plan.movie_path, plan.sync_plan = retimed, sync
            self._move_lines_to_new_times(plan, sync, work_dir)
            self._check_retimed_film(plan, state, sync, info["duration"])
            self._save_sync_plan(plan, state, sync)
        except BaseException:
            self._cleanup_smart_sync(plan)                       # a failed or cancelled run leaves nothing behind
            raise
        return retimed

    # ── where the work is done, and clearing it away ─────────────────────────
    @staticmethod
    def _sync_work_dir(plan: MergePlan) -> str:
        """One folder per film (``safe_id`` is made from the ORIGINAL film's path): two jobs never share or delete each other's."""
        return os.path.join(plan.temp_dir or os.path.abspath("temp"), f"smartsync_{plan.safe_id or 'film'}")

    @staticmethod
    def _cleanup_smart_sync(plan: MergePlan) -> None:
        """The re-timed film is only needed while the merge renders (it is as big as the film); ``paths.clean_temp_after_merge``
        in config.json keeps it when switched off."""
        keep = plan.config_data.get("paths", {}).get("clean_temp_after_merge", True) is False
        if plan.sync_work_dir and not keep:
            shutil.rmtree(plan.sync_work_dir, ignore_errors=True)

    # ── the run-end check ────────────────────────────────────────────────────
    @staticmethod
    def _check_retimed_film(plan: MergePlan, state, sync: smart_sync.SyncPlan, picture_seconds: float) -> None:
        """Says in the log, and keeps with the plan, whether the rendered film is as long as planned and holds the narration."""
        narration_end = max((float(item[3].get("end_sec") or 0.0) for item in plan.audio_items), default=0.0)
        sync.check = smart_sync.verify(sync, picture_seconds, narration_end)
        check = sync.check
        if check["ok"]:
            print(f"[OK] SmartSync: SYNC OK: picture={check['picture_seconds']:.2f}s plan={check['plan_seconds']:.2f}s "
                  f"diff={check['drift_seconds']:+.2f}s | the narration ends at {check['narration_end']:.1f}s")
            return
        message = (f"Smart Sync: the re-timed picture is {check['picture_seconds']:.2f}s but the plan says {check['plan_seconds']:.2f}s "
                   f"(difference {check['drift_seconds']:+.2f}s, allowed {check['tolerance']:.2f}s) and the narration ends at "
                   f"{check['narration_end']:.1f}s: picture and voice may be out of step. Run again, or use the sync mode 'audio_fit'.")
        print(f"[WARN] SmartSync: SYNC DRIFT: {message}")
        warnings = getattr(state, "warnings", None)
        if isinstance(warnings, list) and message not in warnings:
            warnings.append(message)

    # ── the lines, on the new film ────────────────────────────────────────────
    def _move_lines_to_new_times(self, plan: MergePlan, sync: smart_sync.SyncPlan, work_dir: str) -> None:
        """Every line starts where its scene starts on the new film; clips that still did not fit are sped up."""
        items, blocks = [], []
        for index, (sorted_idx, clip, duration, block) in enumerate(plan.audio_items):
            if index in sync.dropped:
                continue
            tempo = sync.voice_tempo.get(index, 1.0)
            if tempo > 1.005:
                faster = os.path.join(work_dir, f"voice_{index + 1:04d}.wav")
                if self._speed_up_clip(clip, tempo, faster):
                    clip, duration = faster, _get_audio_duration(faster) or duration / tempo
            start = sync.block_start(index)
            moved = dict(block)
            moved["start_sec"], moved["end_sec"] = start, start + duration
            items.append((sorted_idx, clip, duration, moved))
            blocks.append(moved)
        plan.audio_items, plan.script_blocks = items, blocks

    def _speed_up_clip(self, clip: str, tempo: float, target: str) -> bool:
        cmd = [_get_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", clip,
               "-filter:a", smart_sync.atempo_filter(tempo), target]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        ok = result.returncode == 0 and os.path.exists(target) and os.path.getsize(target) > 1000
        if not ok:
            print(f"[WARN] SmartSync: could not speed up {os.path.basename(clip)}: {(result.stderr or '')[-200:]}")
        return ok

    # ── the re-timed film ─────────────────────────────────────────────────────
    @staticmethod
    def _worker_count(plan: MergePlan) -> int:
        """
        How many segments are encoded at once. A segment is a few seconds, so most of its time is FFmpeg starting up and the
        encoder opening: several at a time is much faster, up to what the machine does well (a hardware encoder takes a
        few sessions; the CPU encoder already uses every core). ``sync.smart.workers`` in config.json sets it (0 = automatic).
        """
        configured = int(((plan.config_data.get("sync") or {}).get("smart") or {}).get("workers", 0) or 0)
        if configured > 0:
            return min(8, configured)
        cpus = os.cpu_count() or 2
        hardware = not is_cpu_encoder(video_encoder_args(detect_hardware_encoder()))
        return max(1, min(3, cpus if hardware else cpus // 2))

    def _render_retimed_film(self, movie_path: str, sync: smart_sync.SyncPlan, fps: float, target: str, work_dir: str,
                             workers: int = 1) -> None:
        """Encodes every segment on its own (``workers`` at a time), then joins them without re-encoding. RuntimeError when it cannot."""
        has_audio = _has_audio_stream(movie_path)
        encoder = video_encoder_args(detect_hardware_encoder())
        encoders = [encoder] if is_cpu_encoder(encoder) else [encoder, CPU_VIDEO_ARGS]
        total = len(sync.segments)
        pieces = [os.path.join(work_dir, f"seg_{number:05d}.mkv") for number in range(1, total + 1)]
        done = [0]
        lock = threading.Lock()

        def render(number: int) -> None:
            segment = sync.segments[number - 1]
            if _cancelled():
                raise InterruptedError("Smart Sync cancelled by user.")
            if not self._render_segment(movie_path, segment, fps, has_audio, encoders, pieces[number - 1]):
                raise RuntimeError(
                    f"Smart Sync could not re-time the video (segment {number}/{total}, "
                    f"{segment.src_start:.1f}s-{segment.src_end:.1f}s). Run again with the sync mode 'audio_fit' "
                    "(--sync-mode audio_fit), or check the FFmpeg log above.")
            with lock:
                done[0] += 1
                if done[0] == 1 or done[0] % 10 == 0 or done[0] == total:
                    print(f"[*] SmartSync: re-timing the picture, segment {done[0]}/{total}...")

        workers = max(1, min(int(workers), total))
        if workers == 1:
            for number in range(1, total + 1):
                render(number)
        else:
            print(f"[*] SmartSync: {total} segments, {workers} at a time.")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(render, number) for number in range(1, total + 1)]
                try:
                    for future in futures:
                        future.result()                              # in order: the first failure stops the others
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise

        listing = os.path.join(work_dir, "segments.txt")
        with open(listing, "w", encoding="utf-8") as fh:
            for piece in pieces:
                fh.write("file '" + piece.replace("\\", "/").replace("'", r"'\''") + "'\n")
        join = [_get_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                "-i", listing, "-c", "copy", target]
        result = subprocess.run(join, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=max(600, int(sync.out_duration)))
        if result.returncode != 0 or not os.path.exists(target) or os.path.getsize(target) < 1000:
            raise RuntimeError(f"Smart Sync could not join the re-timed segments: {(result.stderr or '')[-300:]}")
        for piece in pieces:
            try:
                os.remove(piece)
            except OSError as exc:
                log_suppressed("agents.merger.smart_sync._render_retimed_film", exc)

    def _render_segment(self, movie_path: str, segment: smart_sync.Segment, fps: float, has_audio: bool,
                        encoders: List[List[str]], target: str) -> bool:
        graph = smart_sync.segment_graph(segment.factor, fps, has_audio)
        maps = ["-map", "[v]"] + (["-map", "[a]", "-c:a", "pcm_s16le", "-ar", "48000"] if has_audio else ["-an"])
        for attempt, video_args in enumerate(encoders, 1):
            cmd = [_get_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
                   "-ss", f"{segment.src_start:.3f}", "-t", f"{segment.src_length:.3f}", "-i", os.path.abspath(movie_path),
                   "-filter_complex", graph, *maps, "-t", f"{segment.out_length:.4f}", *video_args, "-pix_fmt", "yuv420p", target]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                        timeout=max(300, int(segment.src_length * 20)))
            except subprocess.TimeoutExpired:
                result = None
            if result is not None and result.returncode == 0 and os.path.exists(target) and os.path.getsize(target) > 500:
                return True
            if _cancelled():
                raise InterruptedError("Smart Sync cancelled by user.")
            if os.path.exists(target):
                os.remove(target)
            if attempt < len(encoders):
                print("[WARN] SmartSync: the hardware encoder failed on a segment; retrying on the CPU.")
            else:
                print(f"[ERROR] SmartSync: segment {segment.src_start:.1f}s-{segment.src_end:.1f}s failed: "
                      f"{((result.stderr if result else 'timed out') or '')[-300:]}")
        return False

    # ── a track of the original film, e.g. the Demucs background stem ────────
    def _retime_background(self, plan: MergePlan) -> None:
        """The sound-effects stem comes from the ORIGINAL film: it follows the same segments as the picture did."""
        sync: Optional[smart_sync.SyncPlan] = plan.sync_plan
        if sync is None or plan.bg_source_type not in ("demucs", "both") or not plan.bg_audio_file:
            return
        work_dir = self._sync_work_dir(plan)
        os.makedirs(work_dir, exist_ok=True)
        target = os.path.join(work_dir, "background_retimed.wav")
        print("[*] SmartSync: re-timing the background sound effects to match...")
        try:
            self._retime_audio_file(plan.bg_audio_file, sync, target, work_dir)
            plan.bg_audio_file = target
        except Exception as exc:
            if isinstance(exc, InterruptedError):
                raise
            print(f"[WARN] SmartSync: the background effects could not be re-timed ({exc}); using the film's own sound instead.")
            plan.bg_source_type, plan.bg_audio_file = "orig", None

    def _retime_audio_file(self, source: str, sync: smart_sync.SyncPlan, target: str, work_dir: str) -> None:
        pieces: List[str] = []
        for number, segment in enumerate(sync.segments, 1):
            if _cancelled():
                raise InterruptedError("Smart Sync cancelled by user.")
            piece = os.path.join(work_dir, f"bg_{number:05d}.wav")
            graph = (f"[0:a]{smart_sync.atempo_filter(1.0 / segment.factor)},aresample=44100,"
                     "aformat=sample_fmts=s16:channel_layouts=stereo,apad[a]")
            cmd = [_get_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{segment.src_start:.3f}",
                   "-t", f"{segment.src_length:.3f}", "-i", os.path.abspath(source), "-filter_complex", graph,
                   "-map", "[a]", "-t", f"{segment.out_length:.4f}", piece]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode != 0 or not os.path.exists(piece):
                raise RuntimeError((result.stderr or "")[-200:] or "ffmpeg failed")
            pieces.append(piece)
        listing = os.path.join(work_dir, "background.txt")
        with open(listing, "w", encoding="utf-8") as fh:
            for piece in pieces:
                fh.write("file '" + piece.replace("\\", "/").replace("'", r"'\''") + "'\n")
        join = [_get_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                "-i", listing, "-c", "copy", target]
        result = subprocess.run(join, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0 or not os.path.exists(target):
            raise RuntimeError((result.stderr or "")[-200:] or "ffmpeg could not join the segments")
        for piece in pieces:
            try:
                os.remove(piece)
            except OSError as exc:
                log_suppressed("agents.merger.smart_sync._retime_audio_file", exc)

    # ── bookkeeping ───────────────────────────────────────────────────────────
    @staticmethod
    def _warn(state, message: str) -> None:
        warnings = getattr(state, "warnings", None)
        if isinstance(warnings, list) and message not in warnings:
            warnings.append(message)

    @staticmethod
    def _save_sync_plan(plan: MergePlan, state, sync: smart_sync.SyncPlan) -> None:
        """Keeps the plan with the project (``sync_plan.json``) and on the state, so QA can map the script's times."""
        data: Dict[str, Any] = sync.to_dict()
        try:
            with open(os.path.join(plan.output_dir, SYNC_PLAN_NAME), "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
        except OSError as exc:
            log_suppressed("agents.merger.smart_sync._save_sync_plan", exc)
        try:
            state.sync_plan = data
        except Exception as exc:
            log_suppressed("agents.merger.smart_sync._save_sync_plan", exc)
