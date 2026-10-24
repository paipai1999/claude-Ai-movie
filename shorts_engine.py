"""
Long to Short - the best moments of a long video, as Shorts / Reels
===================================================================
Takes a long video (a file or a link) and makes N finished vertical clips from it:

    1. ingest      download / locate the video (same as the Hardsub studio);
    2. transcript  native captions or Faster-Whisper, cached in the project so a re-run does not redo it;
    3. select      Gemini reads the transcript in 12-minute pieces and proposes clips with a score, a title, a
                   caption and hashtags; every proposal is validated (core/clip_selection.py): times snapped to
                   sentence edges, length forced into the limits, overlaps removed. With no working key an
                   offline scorer picks the clips instead;
    4. subtitles   only the lines inside the chosen clips are translated (not the whole film);
    5. render      each clip is cut, laid on a 9:16 / 16:9 canvas, given burnt-in subtitles and a hook title,
                   and loudness-normalised; plus a cover image and an .srt;
    6. report      shorts.json, captions.txt (ready to paste) and state.json.

A Short is a plain edit of the video: there is no mirroring, no zoom trick and no audio shield here.
Re-running the same project with the same selection settings reuses the chosen clips and subtitles and only
re-renders, so changing the subtitle style or the format is quick.
"""

import argparse
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agents.video_merger_agent import _get_safe_ascii_id, detect_hardware_encoder
from brain.diagnostics import log_suppressed
from core import clip_selection as cs
from core import delivery
from core.encoders import CPU_VIDEO_ARGS, is_cpu_encoder, video_encoder_args
from core.letterbox import plan_canvas_mode
from core.paths import resolve_inside, sanitize_project_name
from core.short_ass import build_short_ass, geometry, relative_lines, text_lines_for_clip
from core.subtitle_builder import build_srt_script
from hardsub_engine import HardsubEngine

FONTS_DIR = os.path.join(PROJECT_ROOT, "assets", "fonts")
_WORK_DIR = "_work"

SELECT_SYSTEM = (
    "You are a short-form video editor for TikTok, YouTube Shorts and Facebook Reels. From a timestamped "
    "transcript of a long video you choose the moments that work as stand-alone Shorts.\n"
    "A great clip: opens with a hook in its first 3 seconds (a question, a surprising claim, a conflict), is "
    "understandable without the rest of the video, builds to a payoff, and ends on a complete thought. A clip "
    "never starts or ends in the middle of a sentence, and never needs the viewer to have seen anything before it.\n"
    "Answer with JSON only."
)

FOCUS_DIRECTIVES = {
    "balanced": "the strongest moments of any kind: surprising, emotional, funny or useful.",
    "funny": "funny moments: jokes, awkward situations, unexpected reactions and punchlines.",
    "emotional": "emotional moments: confessions, conflict, heartbreak, triumph and turning points.",
    "informative": "moments that teach something useful in under a minute: a tip, an explanation, a surprising fact.",
    "action": "high-energy moments: chases, fights, danger, big reveals and fast reversals.",
}

SELECT_USER = """VIDEO LENGTH: {length}
CLIP LENGTH: between {min_sec:g} and {max_sec:g} seconds.
LOOK FOR: {focus}
{note}TRANSCRIPT PART {part} of {parts} (each line is [start-end] in mm:ss from the start of the whole video):
{transcript}

Return up to {wanted} clips from THIS part as a JSON array. Each item:
{{"start": "mm:ss", "end": "mm:ss", "score": 0-100, "title": "...", "hook": "...", "reason": "...", "caption": "...", "hashtags": ["...", "..."]}}
Rules:
- start and end must be copied from the line timestamps above, so the clip begins and ends on a full line.
- title: a catchy title of at most 10 words. hook: the single line that should appear on screen first.
- caption: a ready-to-post caption of 1-2 sentences. hashtags: 3-6 relevant tags without spaces.
- Write title, hook and caption in {language}. Write reason in English, one short sentence.
- Score honestly: 90 or more only for truly exceptional moments. Return [] if nothing in this part is good enough."""


def _read_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def clean_clips_dir() -> str:
    """Folder (inside the project) of the clean clips the CapCut pipeline writes."""
    from services.capcut_export import CLEAN_CLIPS_DIR
    return CLEAN_CLIPS_DIR


def _escape_filter_path(value: str) -> str:
    """A path inside an FFmpeg filter argument: forward slashes, with quotes and the drive colon escaped."""
    return value.replace("\\", "/").replace("'", r"\'").replace(":", r"\:")


class ShortsEngine(HardsubEngine):
    """
    Long to Short. It *is* a hardsub studio run on selected windows of the film: ingest, transcript, translation
    and the shared ffmpeg / key plumbing come from ``HardsubEngine``; selection and rendering are added here.
    """

    cancel_message = "ShortsEngine execution cancelled by user."

    # ── 1. ingest ────────────────────────────────────────────────────────────
    def _ingest_video(self, input_source: str, project_dir: str, force_whisper: bool = False):
        """
        A local file is used where it is: copying a multi-gigabyte source into every project only doubles the disk
        use. A link is downloaded into the project (the hardsub studio's own behaviour) so a re-run does not fetch it again.
        """
        if self.is_url(input_source):
            return super()._ingest_video(input_source, project_dir, force_whisper)
        self._check_cancellation()
        local = os.path.abspath(input_source)
        if not os.path.isfile(local):
            raise FileNotFoundError(f"Input video file not found: {local}")
        print(f"[*] Input is local video -> {local}")
        title = os.path.splitext(os.path.basename(local))[0]
        meta = self._probe_video_metadata(local)
        meta["title"] = title
        print(f"[OK] Ingestion ready: '{title}' ({float(meta.get('duration') or 0.0):.1f}s, {meta.get('width')}x{meta.get('height')})")
        return local, None, title, meta

    # ── 2. transcript ────────────────────────────────────────────────────────
    def _cached_transcript(self, video_file: str, sub_file: Optional[str], source_language: str,
                           meta: Dict[str, Any], project_dir: str) -> List[Dict[str, Any]]:
        """The transcript, reused from the project when the same video and language were already transcribed."""
        cache = os.path.join(project_dir, "source_transcript.json")
        signature = {"size": os.path.getsize(video_file), "duration": round(float(meta.get("duration") or 0.0), 1),
                     "language": source_language}
        cached = _read_json(cache)
        if isinstance(cached, dict) and cached.get("signature") == signature and cached.get("segments"):
            print(f"[OK] Reusing the saved transcript ({len(cached['segments'])} lines).")
            return cached["segments"]
        segments, _extractor = self._extract_transcript(video_file, sub_file, source_language)
        _write_json(cache, {"signature": signature, "segments": segments})
        return segments

    # ── 3. selection ─────────────────────────────────────────────────────────
    def _ask_model(self, chunk: List[Dict[str, Any]], part: int, parts: int, duration: float, min_sec: float,
                   max_sec: float, focus: str, hint: Optional[str], language: str, wanted: int) -> List[Any]:
        """One transcript piece -> the model's clip proposals (``[]`` when it cannot answer)."""
        keys = self._get_api_keys()
        model = self.config_data.get("gemini", {}).get("models", {}).get("workhorse", "gemini-3.5-flash-lite")
        note = f"CREATOR NOTE (what a good clip means for this channel): {hint.strip()}\n" if hint and hint.strip() else ""
        user = SELECT_USER.format(
            length=cs.clock(duration), min_sec=min_sec, max_sec=max_sec, focus=FOCUS_DIRECTIVES.get(focus, FOCUS_DIRECTIVES["balanced"]),
            note=note, part=part, parts=parts, transcript=cs.format_transcript(chunk), wanted=wanted, language=language,
        )
        for attempt in range(2):
            self._check_cancellation()
            try:
                raw, _model = self._call_llm(system_prompt=SELECT_SYSTEM, user_prompt=user, api_key=keys, model=model,
                                             temperature=0.4, response_mime_type="application/json")
                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip(), flags=re.MULTILINE).strip()
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict):
                    parsed = parsed.get("clips") or parsed.get("moments") or []
                if isinstance(parsed, list):
                    return parsed
            except Exception as exc:
                print(f"[WARN] Clip selection, part {part}/{parts}, attempt {attempt + 1}: {exc}")
                time.sleep(2)
        return []

    def _select_clips(self, segments: List[Dict[str, Any]], duration: float, count: int, min_sec: float,
                      max_sec: float, focus: str, hint: Optional[str], language: str) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        if self._get_api_keys():
            chunks = cs.chunk_segments(segments)
            wanted = min(8, max(2, math.ceil(count * 2 / max(1, len(chunks)))))
            for number, chunk in enumerate(chunks, 1):
                print(f"[*] Asking the AI for clips: part {number}/{len(chunks)}...", flush=True)
                for item in self._ask_model(chunk, number, len(chunks), duration, min_sec, max_sec, focus, hint, language, wanted):
                    clip = cs.normalize_candidate(item, segments, duration, min_sec, max_sec)
                    if clip:
                        candidates.append(clip)
        else:
            print("[WARN] No Gemini API key: picking clips with the offline scorer instead of the AI.")

        offline = cs.heuristic_candidates(segments, min_sec, max_sec, focus)
        if not candidates:
            if self._get_api_keys():
                print("[WARN] The AI proposed no usable clip: falling back to the offline scorer.")
            return cs.choose_clips(offline, count)
        chosen = cs.choose_clips(candidates, count)
        if len(chosen) < count:
            chosen = cs.top_up(chosen, offline, count)
            print(f"[*] The AI found {len(candidates)} usable clip(s); added offline picks to reach {len(chosen)}.")
        return chosen

    # ── 4. subtitles ─────────────────────────────────────────────────────────
    def _clip_lines(self, segments: List[Dict[str, Any]], clips: List[Dict[str, Any]], mode: str,
                    source_language: str, translation_style: str, hint: Optional[str]) -> None:
        """Adds ``lines`` (text to show, in source-video time) to every clip; translates only what the clips need."""
        if mode == "none":
            for clip in clips:
                clip["lines"] = []
            return
        needed = [dict(s) for s in segments
                  if any(float(s["end"]) > c["start"] and float(s["start"]) < c["end"] for c in clips)]
        print(f"[*] {len(needed)} of {len(segments)} transcript lines are inside the chosen clips.")
        if mode == "burmese":
            self._translate_dialogue(needed, source_language, translation_style=translation_style, context_hint=hint)
        else:
            for segment in needed:
                segment["burmese"] = segment.get("original", "")
        for clip in clips:
            clip["lines"] = text_lines_for_clip(needed, clip["start"], clip["end"], field="burmese")

    # ── 5. rendering ─────────────────────────────────────────────────────────
    def _filter_graph(self, fmt: str, out_w: int, out_h: int, framing: str, meta: Dict[str, Any], ass_name: str) -> str:
        """The video filter graph: canvas for the format, then the subtitles. Output label ``[vout]``."""
        ass = f"ass=filename='{_escape_filter_path(ass_name)}':shaping=1:fontsdir='{_escape_filter_path(FONTS_DIR)}'"
        if fmt == "9:16" and framing == "fill":
            # Fill the whole screen from the middle of the picture (good for one person talking to the camera).
            return (f"[0:v]crop='trunc(min(iw,ih*9/16)/2)*2':ih,scale={out_w}:{out_h}:flags=lanczos,setsar=1,{ass}[vout]")
        if fmt == "16:9" and plan_canvas_mode(meta.get("width"), meta.get("height"), out_w, out_h) == "direct":
            return f"[0:v]scale={out_w}:{out_h},setsar=1,{ass}[vout]"
        # The whole picture, fitted inside the canvas over a blurred copy of itself (nothing is cropped away).
        bg_w, bg_h = out_w // 4, out_h // 4
        return (f"[0:v]split=2[bg_src][fg_src];"
                f"[bg_src]scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,crop={bg_w}:{bg_h},boxblur=12:3,"
                f"scale={out_w}:{out_h}[bg];"
                f"[fg_src]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[canvas];[canvas]setsar=1,{ass}[vout]")

    def _render_clip(self, video_file: str, clip: Dict[str, Any], fmt: str, target: str, work_dir: str, meta: Dict[str, Any],
                     resolution: str, preset: str, framing: str, hook: bool, has_audio: bool) -> bool:
        out_w, out_h = geometry(fmt, resolution)[:2]
        length = clip["end"] - clip["start"]
        ass_name = f"short_{clip['index']:02d}_{fmt.replace(':', 'x')}.ass"
        with open(os.path.join(work_dir, ass_name), "w", encoding="utf-8") as fh:
            fh.write(build_short_ass(clip.get("lines", []), clip["start"], clip["end"], fmt, resolution, preset,
                                     title=(clip.get("hook") or clip.get("title")) if hook else None))
        graph = self._filter_graph(fmt, out_w, out_h, framing, meta, ass_name)
        audio: List[str] = []
        if has_audio:
            fade_out = max(0.0, length - 0.25)
            audio = ["-af", f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.15,afade=t=out:st={fade_out:.2f}:d=0.25"]

        def command(video_args: List[str]) -> List[str]:
            return [self.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{clip['start']:.3f}", "-i", os.path.abspath(video_file), "-t", f"{length:.3f}",
                    "-filter_complex", graph, "-map", "[vout]", "-map", "0:a?", *audio,
                    *video_args, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                    "-movflags", "+faststart", os.path.abspath(target)]

        return self._encode(command, target, f"Short {clip['index']} ({fmt})", max(900, int(length * 30)), cwd=work_dir)

    def _render_clean_clip(self, video_file: str, clip: Dict[str, Any], target: str) -> bool:
        """
        The CapCut pipeline's clip: an exact cut of the source in its own picture format, with its own audio, and
        nothing burnt in - the creator adds captions, effects and framing in CapCut.
        """
        length = clip["end"] - clip["start"]

        def command(video_args: List[str]) -> List[str]:
            return [self.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{clip['start']:.3f}", "-i", os.path.abspath(video_file), "-t", f"{length:.3f}",
                    "-map", "0:v:0", "-map", "0:a?", *video_args, "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", os.path.abspath(target)]

        return self._encode(command, target, f"clean clip {clip['index']}", max(900, int(length * 20)))

    def _encode(self, command, target: str, label: str, timeout: int, cwd: Optional[str] = None) -> bool:
        """Runs ``command(video_encoder_args)`` with the hardware encoder first and the CPU encoder as the fallback."""
        encoder = video_encoder_args(detect_hardware_encoder())
        attempts = [encoder] if is_cpu_encoder(encoder) else [encoder, CPU_VIDEO_ARGS]
        for attempt, video_args in enumerate(attempts, 1):
            try:
                res = subprocess.run(command(video_args), cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", timeout=timeout)
            except subprocess.TimeoutExpired:
                res = None
            if res is not None and res.returncode == 0 and os.path.exists(target) and os.path.getsize(target) > 1000:
                return True
            self._check_cancellation()                      # a killed FFmpeg (Stop button) must not trigger a retry
            if os.path.exists(target):
                os.remove(target)
            tail = "\n".join(line for line in ((res.stderr if res else "timed out") or "").splitlines() if line.strip())[-300:]
            if attempt < len(attempts):
                print(f"[WARN] Hardware encoder failed on {label}; retrying on the CPU. {tail}")
            else:
                print(f"[ERROR] {label} could not be rendered: {tail}")
        return False

    def _make_cover(self, video_path: str, cover_path: str, length: float) -> Optional[str]:
        """A JPEG cover from the rendered Short (one second in, when the hook title is on screen)."""
        cmd = [self.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{min(1.0, length / 2):.2f}",
               "-i", video_path, "-frames:v", "1", "-q:v", "3", cover_path]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception as exc:
            log_suppressed("shorts_engine._make_cover", exc)
        return cover_path if os.path.exists(cover_path) and os.path.getsize(cover_path) > 0 else None

    # ── run ──────────────────────────────────────────────────────────────────
    def run(
        self,
        input_source: str,
        project_name: Optional[str] = None,
        source_language: str = "auto",
        clip_count: int = 5,
        clip_min_sec: float = 20.0,
        clip_max_sec: float = 60.0,
        clip_focus: str = "balanced",
        short_subtitles: str = "burmese",
        short_hook_title: bool = True,
        short_framing: str = "fit",
        translation_style: str = "dialogue",
        subtitle_style: str = "box_black",
        video_format: str = "9:16",
        resolution: str = "1080p",
        context_hint: Optional[str] = None,
        delivery_mode: str = "oneclick",
    ) -> Dict[str, Any]:
        started = time.time()
        mode = delivery.normalize(delivery_mode)
        make_finished = delivery.wants_video(mode)      # burnt-in, finished Shorts
        make_clean = delivery.wants_package(mode)       # clean clips for CapCut + the package
        count = max(1, min(20, int(clip_count)))
        min_sec = max(8.0, float(clip_min_sec))
        max_sec = max(min_sec + 5.0, float(clip_max_sec))
        safe_id = _get_safe_ascii_id(project_name or input_source, prefix="proj")
        clean_name = sanitize_project_name(project_name, fallback=f"shorts_{safe_id}")
        project_dir = resolve_inside(self.output_base_dir, clean_name)
        if not project_dir:
            raise ValueError(f"Invalid project name: {project_name!r}")
        os.makedirs(project_dir, exist_ok=True)

        print(f"\nShortsEngine: Initializing '{clean_name}'...")
        print(f"   Clips: {count} x {min_sec:g}-{max_sec:g}s | Focus: {clip_focus} | Subtitles: {short_subtitles} | "
              f"Format: {video_format} ({short_framing}) | Style: {subtitle_style} | Delivery: {mode}")

        print("\n--- [Phase 1: Downloading & Ingesting Video] ---", flush=True)
        video_file, sub_file, title, meta = self._ingest_video(input_source, project_dir, force_whisper=False)
        duration = float(meta.get("duration") or 0.0)
        if duration < min_sec:
            raise ValueError(f"The video ({cs.clock(duration)}) is shorter than the shortest clip ({min_sec:g} s).")
        print("[PROGRESS] 10", flush=True)

        print("\n--- [Phase 2: Extracting Audio & Timestamps] ---", flush=True)
        segments = self._cached_transcript(video_file, sub_file, source_language, meta, project_dir)
        print("[PROGRESS] 30", flush=True)

        print("\n--- [Phase 3: Choosing the best moments] ---", flush=True)
        language = "Burmese (Myanmar Unicode)" if short_subtitles == "burmese" else "English"
        plan_key = {"count": count, "min": min_sec, "max": max_sec, "focus": clip_focus, "hint": context_hint or "",
                    "source_language": source_language, "translation_style": translation_style,
                    "subtitles": short_subtitles, "duration": round(duration, 1)}
        plan_path = os.path.join(project_dir, "clips_plan.json")
        plan = _read_json(plan_path)
        if isinstance(plan, dict) and plan.get("key") == plan_key and plan.get("clips"):
            clips = plan["clips"]
            print(f"[OK] Reusing the {len(clips)} clips chosen earlier (same selection settings).")
        else:
            clips = self._select_clips(segments, duration, count, min_sec, max_sec, clip_focus, context_hint, language)
            if not clips:
                raise RuntimeError("No clip could be chosen: the transcript is too short or has no speech.")
            print("\n--- [Phase 4: Translating the clip subtitles] ---", flush=True)
            self._clip_lines(segments, clips, short_subtitles, source_language, translation_style, context_hint)
            _write_json(plan_path, {"key": plan_key, "clips": clips})
        for clip in clips:
            print(f"   #{clip['index']}  {cs.clock(clip['start'])}-{cs.clock(clip['end'])}  score {clip['score']:.0f}  "
                  f"[{clip['source']}]  {clip.get('title') or ''}")
        print("[PROGRESS] 55", flush=True)

        formats = ["16:9", "9:16"] if video_format == "both" else [video_format]
        work_dir = os.path.join(project_dir, _WORK_DIR)
        os.makedirs(work_dir, exist_ok=True)
        if make_clean:
            os.makedirs(os.path.join(project_dir, clean_clips_dir()), exist_ok=True)
        rendered: Dict[str, str] = {}
        manifest: List[Dict[str, Any]] = []
        total_jobs = len(clips) * ((len(formats) if make_finished else 0) + (1 if make_clean else 0))
        done_jobs = 0
        has_audio = bool(meta.get("has_audio", True))
        for clip in clips:
            files: Dict[str, str] = {}
            clean_file: Optional[str] = None
            if make_finished:
                for fmt in formats:
                    self._check_cancellation()
                    print(f"\n--- [Phase 5: Rendering Short {clip['index']}/{len(clips)} ({fmt})] ---", flush=True)
                    name = f"short_{clip['index']:02d}" + ("" if fmt == "9:16" else "_16x9") + ".mp4"
                    if self._render_clip(video_file, clip, fmt, os.path.join(project_dir, name), work_dir, meta,
                                         resolution, subtitle_style, short_framing, short_hook_title, has_audio):
                        files[fmt] = name
                        rendered[f"{clip['index']:02d} {fmt}"] = os.path.join(project_dir, name)
                    done_jobs += 1
                    print(f"[PROGRESS] {55 + int(40 * done_jobs / max(1, total_jobs))}", flush=True)
            if make_clean:
                self._check_cancellation()
                print(f"\n--- [Phase 5: Cutting clean clip {clip['index']}/{len(clips)} for CapCut] ---", flush=True)
                relative = f"{clean_clips_dir()}/clip_{clip['index']:02d}.mp4"
                if self._render_clean_clip(video_file, clip, os.path.join(project_dir, relative)):
                    clean_file = relative
                    rendered[f"{clip['index']:02d} clean"] = os.path.join(project_dir, relative)
                done_jobs += 1
                print(f"[PROGRESS] {55 + int(40 * done_jobs / max(1, total_jobs))}", flush=True)
            if not files and not clean_file:
                continue
            cover_source = (files.get("9:16") or next(iter(files.values()), None)) or clean_file
            cover = self._make_cover(os.path.join(project_dir, cover_source), os.path.join(project_dir, f"short_{clip['index']:02d}.jpg"),
                                     clip["end"] - clip["start"])
            subtitle_file = self._write_clip_srt(project_dir, clip)
            manifest.append(dict({k: v for k, v in clip.items() if k != "lines"}, files=files, clean_file=clean_file,
                                 cover=os.path.basename(cover) if cover else None, subtitles=subtitle_file))

        shutil.rmtree(work_dir, ignore_errors=True)
        elapsed = time.time() - started
        if not manifest:
            return {"status": "failed", "error": "No Short could be rendered.", "project_dir": project_dir,
                    "duration_sec": elapsed}

        print("\n--- [Phase 6: Exporting the report] ---", flush=True)
        self._write_reports(project_dir, title, duration, manifest, plan_key, video_format, mode)
        package = delivery.build_package(project_dir, mode)       # capcut / both: capcut_package.zip
        print("[PROGRESS] 100", flush=True)
        print("\n" + "=" * 65)
        print(f"ShortsEngine COMPLETED in {elapsed:.1f}s: {len(manifest)} Short(s) in {project_dir}")
        print("=" * 65 + "\n")
        return {
            "status": "completed",
            "project_dir": project_dir,
            "clips": manifest,
            "rendered_videos": rendered,
            "duration_sec": elapsed,
            "total_records": len(manifest),
            "phase": f"Made {len(manifest)} Short(s)",
            "package": package["zip"] if package else None,
        }

    # ── 6. report ────────────────────────────────────────────────────────────
    @staticmethod
    def _write_clip_srt(project_dir: str, clip: Dict[str, Any]) -> Optional[str]:
        """An .srt of the clip's subtitles with a UTF-8 BOM (CapCut needs it for Burmese), or None without subtitles."""
        rows = [{"start": s, "end": e, "burmese": text} for s, e, text in relative_lines(clip.get("lines", []), clip["start"], clip["end"])]
        if not rows:
            return None
        name = f"short_{clip['index']:02d}.srt"
        with open(os.path.join(project_dir, name), "w", encoding="utf-8-sig", newline="\n") as fh:
            fh.write(build_srt_script(rows, text_key="burmese"))
        return name

    def _write_reports(self, project_dir: str, title: str, duration: float, manifest: List[Dict[str, Any]],
                       plan_key: Dict[str, Any], video_format: str, mode: str = "oneclick") -> None:
        _write_json(os.path.join(project_dir, "shorts.json"),
                    {"source": title, "duration": round(duration, 2), "settings": plan_key, "format": video_format,
                     "delivery_mode": mode, "clips": manifest})
        blocks = []
        for clip in manifest:
            blocks.append("\n".join(filter(None, [
                f"SHORT {clip['index']:02d}   {cs.clock(clip['start'])} - {cs.clock(clip['end'])}   score {clip['score']:.0f}",
                f"Title:    {clip['title']}" if clip.get("title") else "",
                f"Caption:  {clip['caption']}" if clip.get("caption") else "",
                f"Hashtags: {' '.join(clip['hashtags'])}" if clip.get("hashtags") else "",
                f"Why:      {clip['reason']}" if clip.get("reason") else "",
                "Files:    " + ", ".join(list(clip["files"].values()) + ([clip["clean_file"]] if clip.get("clean_file") else [])),
            ])))
        with open(os.path.join(project_dir, "captions.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"{title}\n{'=' * len(title)}\n\n" + "\n\n".join(blocks) + "\n")

        state = {
            "movie_name": title,
            "engine_type": "shorts",
            "delivery_mode": mode,
            "pipeline_status": "COMPLETED",
            "progress": 100,
            "total_records": len(manifest),
            "duration": duration,
            "total_duration_formatted": cs.clock(duration),
            "updated_at": datetime.datetime.now().isoformat(),
        }
        _write_json(os.path.join(project_dir, "state.json"), state)
        try:
            from brain.sqlite_store import save_custom_movie_state
            save_custom_movie_state(
                project_dir=os.path.basename(os.path.normpath(project_dir)), movie_name=title, movie_path="",
                language="burmese", whisper_model="-", progress=100, current_phase="Completed",
                state_dict=state, output_dir=self.output_base_dir,
            )
        except Exception as exc:
            log_suppressed("shorts_engine._write_reports", exc)


def main():
    from core.cli import SHORTS, add_option_arguments, resolve_args

    parser = argparse.ArgumentParser(description="Long to Short - the best moments of a long video as Shorts / Reels")
    parser.add_argument("input", help="Video path or URL")
    parser.add_argument("--cookies", default=None, help="Path to cookies.txt file for YouTube download")
    add_option_arguments(parser, SHORTS)
    args = parser.parse_args()
    v = resolve_args(SHORTS, args)

    engine = ShortsEngine(cookies_path=args.cookies)
    result = engine.run(
        input_source=args.input,
        project_name=v["project_name"],
        source_language=v["source_language"],
        clip_count=v["clip_count"],
        clip_min_sec=v["clip_min_sec"],
        clip_max_sec=v["clip_max_sec"],
        clip_focus=v["clip_focus"],
        short_subtitles=v["short_subtitles"],
        short_hook_title=v["short_hook_title"],
        short_framing=v["short_framing"],
        translation_style=v["translation_style"],
        subtitle_style=v["subtitle_style"],
        video_format=v["video_format"],
        resolution=v["resolution"],
        context_hint=v["context_hint"],
        delivery_mode=v["delivery_mode"],
    )
    if isinstance(result, dict) and result.get("status") == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
