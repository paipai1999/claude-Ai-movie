"""
Job Runtime (အလုပ် လည်ပတ်မှု စနစ်)
================================
Everything needed to RUN a dashboard job off the HTTP thread:

  - ThreadedStdout      : console output -> per-job log buffer + live SSE subscribers
  - _run_job            : one lifecycle (cancel / done / error bookkeeping) for every worker
  - *_worker functions  : Recap, Subtitle, Hardsub, Batch and Download workers

web_ui.py only wires these into HTTP endpoints; nothing here touches FastAPI.

Dashboard မှ စတင်သော အလုပ်များကို နောက်ကွယ်တွင် လည်ပတ်စေသည့် စနစ်များ (stdout ဖမ်းခြင်း၊ job lifecycle၊
worker များ) ကို ဤ module တွင် စုစည်းထားသည်။
"""

import contextvars
import io
import os
import sys
import threading
import traceback
from typing import Any, Callable, Dict, Optional

from agents import gemini_tts
from agents.downloader_agent import DownloaderAgent
from agents.master import MasterAgent
from brain.diagnostics import log_suppressed
from brain.sqlite_store import create_job, update_job
from services import process_registry
from services import projects as project_store
from services.job_args import options_from_call
from services.job_manager import jobs, jobs_lock

VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v')
# Local inputs accepted by the pipelines (uploads/listing keep the stricter VIDEO_EXTENSIONS)
INPUT_EXTENSIONS = VIDEO_EXTENSIONS + ('.ts', '.mpg', '.mpeg', '.wmv', '.3gp')

# Cancellation signal events: {job_id: threading.Event}
cancel_events: Dict[str, threading.Event] = {}


# ContextVar for thread-local job identification across worker thread pools (Python 3.7+)
current_job_id = contextvars.ContextVar("current_job_id", default=None)


def _job_for_new_process() -> Optional[str]:
    """
    Which job should own a subprocess that is being spawned right now?
    The thread's own job id when known; otherwise the one running job (worker threads created by
    third-party libraries do not inherit the context variable).
    """
    job_id = current_job_id.get()
    if job_id:
        return job_id
    with jobs_lock:
        running = [jid for jid, j in jobs.items() if j.get("status") == "running"]
    return running[0] if len(running) == 1 else None


process_registry.install(_job_for_new_process)

# Owner id of /api/downloader/video requests (they run outside the job queue)
_STANDALONE_DOWNLOAD_JOB = "__standalone_download__"



class ThreadedStdout:
    """
    Custom multiplexing stdout stream that routes console logs simultaneously to:
      1. Original system stdout / console
      2. Per-job in-memory StringIO buffer (for later download/inspection)
      3. Connected SSE subscribers (for zero-latency browser terminal feeds)
      
    Console တွင် ပေါ်သည့် စာသားများကို Web UI အချိန်နှင့်တပြေးညီ မြင်တွေ့နိုင်ရန်
    StringIO buffer နှင့် SSE browser queues များဆီသို့ တစ်ပြိုင်နက် ပို့ဆောင်ပေးသည်။
    """
    def __init__(self, original_stdout):
        self.original_stdout = original_stdout
        self.buffers: Dict[str, io.StringIO] = {}
        self.subscribers: Dict[str, list] = {}

    def write(self, s: str):
        # Ignore HTTP polling access logs from being written into job buffers
        is_http_poll = any(ign in s for ign in ("GET /api/status", "GET /api/queue", "GET /api/keys", "OPTIONS /api/"))

        jid = current_job_id.get()
        if not jid and self.buffers and not is_http_poll:
            # Fallback: associate with active job buffer if context was not inherited
            if len(self.buffers) == 1:
                jid = next(iter(self.buffers.keys()))
            else:
                jid = list(self.buffers.keys())[-1]

        if jid and jid in self.buffers and not is_http_poll:
            try:
                self.buffers[jid].write(s)
            except Exception:
                pass
            if jid in self.subscribers:
                for item in list(self.subscribers.get(jid, [])):
                    try:
                        if isinstance(item, tuple):
                            q, loop = item
                            if loop.is_running():
                                loop.call_soon_threadsafe(q.put_nowait, s)
                        else:
                            item.put_nowait(s)
                    except Exception:
                        pass
        try:
            self.original_stdout.write(s)
            self.original_stdout.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self.original_stdout.flush()
        except Exception:
            pass

    def isatty(self):
        return getattr(self.original_stdout, "isatty", lambda: False)()

    def fileno(self):
        if hasattr(self.original_stdout, "fileno"):
            return self.original_stdout.fileno()
        raise io.UnsupportedOperation("fileno")

    def reconfigure(self, **kwargs):
        if hasattr(self.original_stdout, "reconfigure"):
            self.original_stdout.reconfigure(**kwargs)

    def __getattr__(self, name):
        return getattr(self.original_stdout, name)


# The console multiplexer. web_ui.py installs it as sys.stdout / sys.stderr when the server starts.
thread_stdout = ThreadedStdout(sys.stdout)


def _resolve_input_source(input_source: str) -> str:
    """
    Resolves input filename or URL to a canonical local filesystem path or validated URL.
    - If URL: returns as-is.
    - If relative filename: checks inside `movies/` directory.
    - If absolute path: verifies file existence.
    
    ဗီဒီယိုဖိုင် အမည် သို့မဟုတ် URL အား စစ်ဆေးပြီး တိကျသော File Path ကို ရှာဖွေပေးသည်။
    """
    source = str(input_source or '').strip()
    if not source:
        raise ValueError("No input provided")
    if DownloaderAgent.is_url(source):
        return source
    if os.path.exists(source):
        # Only media files may be processed – never feed arbitrary local files to FFmpeg/Whisper.
        if not source.lower().endswith(INPUT_EXTENSIONS):
            raise ValueError(f"Unsupported input type: '{os.path.basename(source)}' (video files only)")
        return os.path.abspath(source)
    if not os.path.dirname(source):
        movies_path = os.path.join('movies', source)
        if os.path.exists(movies_path):
            return os.path.abspath(movies_path)
    raise FileNotFoundError(f"File not found: '{source}'")


# ─────────────────────────────────────────────────────────────────────────────
# Shared job lifecycle (အလုပ်အားလုံး အတွက် တူညီသော စတင်/ပြီးဆုံး/ရပ်တန့် စီမံမှု)
# ─────────────────────────────────────────────────────────────────────────────
# Every worker below only describes WHAT to run; this block owns HOW a job is tracked:
# log buffer registration, cancel detection, done/error/cancelled bookkeeping (memory + SQLite).

def _is_job_cancelled(job_id: str) -> bool:
    """True when the job's cancel event or the process-wide stop flag is set."""
    ev = cancel_events.get(job_id)
    return bool((ev and ev.is_set()) or os.environ.get("CURRENT_JOB_CANCELLED") == "1")


def _set_job_state(job_id: str, status: Optional[str] = None, phase: Optional[str] = None, **extra) -> None:
    """Updates a job in memory and in the SQLite job table (DB failures are non-fatal)."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            if status:
                job['status'] = status
            if phase:
                job['phase'] = phase
            job.update(extra)
    try:
        update_job(job_id, status=status, phase=phase)
    except Exception as _exc:
        log_suppressed("web_ui._set_job_state", _exc)


def _project_dir_from_result(result: Any) -> Optional[str]:
    """
    Derives the project folder (relative to outputs/) from an engine result so the ZIP endpoint
    can bundle exactly this job's files. Understands the `project_dir` key (HardsubEngine / Master)
    and a {name: file_path} mapping (SubtitleEngine).
    """
    if not isinstance(result, dict):
        return None
    candidates = []
    if isinstance(result.get("project_dir"), str):
        candidates.append(result["project_dir"])
    candidates += [os.path.dirname(v) for v in result.values() if isinstance(v, str) and os.path.isabs(v)]
    outputs_root = os.path.abspath("outputs")
    for cand in candidates:
        full = os.path.abspath(cand if os.path.isabs(cand) else os.path.join("outputs", cand))
        if full == outputs_root or not full.startswith(outputs_root + os.sep):
            continue
        return os.path.relpath(full, outputs_root).replace(os.sep, "/")
    return None


def _run_job(job_id: str, source: Any, start_phase: str, work: Callable[[], Any], db_status: Optional[str] = None) -> None:
    """
    Runs `work()` inside the standard job lifecycle.

    `work` may return:
      - a dict with "status": "failed" -> the job is recorded as an error (never a fake "done"),
      - a dict / path-mapping from which the project folder is derived for ZIP download.
    """
    current_job_id.set(job_id)
    cancel_events[job_id] = threading.Event()
    os.environ["CURRENT_JOB_CANCELLED"] = "0"
    buffer = io.StringIO()
    thread_stdout.buffers[job_id] = buffer
    with jobs_lock:
        jobs[job_id]['buffer'] = buffer

    try:
        if db_status:
            create_job(job_id, str(source), phase=start_phase, status=db_status)
        else:
            create_job(job_id, str(source), phase=start_phase)
    except Exception as _exc:
        log_suppressed("web_ui._run_job", _exc)

    try:
        result = work()
        if _is_job_cancelled(job_id):
            _set_job_state(job_id, 'cancelled', 'Stopped by user')
        elif isinstance(result, dict) and result.get("status") == "failed":
            raise RuntimeError(str(result.get("error") or "Pipeline reported failure"))
        else:
            extra = {}
            project_dir = _project_dir_from_result(result)
            if project_dir:
                extra["project_dir"] = project_dir
                if isinstance(result, dict) and result.get("package"):
                    extra["package"] = project_dir          # the dashboard downloads capcut_package.zip for this job
            done_phase = result.get("phase") if isinstance(result, dict) and isinstance(result.get("phase"), str) else "Done"
            _set_job_state(job_id, 'done', done_phase, **extra)
    except Exception as e:
        if isinstance(e, InterruptedError) or _is_job_cancelled(job_id):
            print(f"\n[STOP] Job {job_id} was force-stopped by user.")
            _set_job_state(job_id, 'cancelled', 'Stopped by user')
        else:
            traceback.print_exc()
            err_msg = str(e) or type(e).__name__
            _set_job_state(job_id, 'error', f"Error: {err_msg[:60]}", error=err_msg)
    finally:
        # Free the log buffer and forget the job's child processes immediately on job end
        thread_stdout.buffers.pop(job_id, None)
        process_registry.release(job_id)


def _apply_pipeline_env(skip_demucs: bool, detect_scenes: bool, video_format: str, reels_enabled: Optional[bool]) -> None:
    """Translates per-job options into the env flags read by the recap pipeline agents."""
    if skip_demucs:
        os.environ["SKIP_DEMUCS"] = "true"
    else:
        os.environ.pop("SKIP_DEMUCS", None)

    os.environ["SKIP_SCENES"] = "0" if detect_scenes else "1"

    if video_format == "16:9" or reels_enabled is False:
        os.environ["DISABLE_REELS"] = "true"
        os.environ.pop("ENABLE_REELS", None)
    elif video_format in ["9:16", "both"] or reels_enabled is True:
        os.environ["ENABLE_REELS"] = "true"
        os.environ.pop("DISABLE_REELS", None)


def _resolve_voice_mapping(language: str, tts_voice: Optional[str]) -> tuple:
    """
    Maps UI language / voice shortcuts to (clean_language, edge_tts_voice_name).
    UI မှ ရွေးချယ်သော language / voice အတိုကောက်များကို Edge-TTS voice အမည်အဖြစ် ပြောင်းပေးသည်။
    """
    voice_aliases = {
        "thiha": "my-MM-ThihaNeural", "male": "my-MM-ThihaNeural", "burmese_thiha": "my-MM-ThihaNeural",
        "nilar": "my-MM-NilarNeural", "female": "my-MM-NilarNeural", "burmese_nilar": "my-MM-NilarNeural",
        "guy": "en-US-GuyNeural", "english_guy": "en-US-GuyNeural",
        "jenny": "en-US-JennyNeural", "english_jenny": "en-US-JennyNeural",
        **gemini_tts.SHORTCUTS,
    }
    language_defaults = {
        "burmese_thiha": ("burmese", "my-MM-ThihaNeural"), "thiha": ("burmese", "my-MM-ThihaNeural"),
        "burmese_nilar": ("burmese", "my-MM-NilarNeural"), "nilar": ("burmese", "my-MM-NilarNeural"),
        "burmese": ("burmese", "my-MM-ThihaNeural"), "mm": ("burmese", "my-MM-ThihaNeural"),
        "myanmar": ("burmese", "my-MM-ThihaNeural"),   # auto multi-voice enabled
        "english_guy": ("english", "en-US-GuyNeural"), "guy": ("english", "en-US-GuyNeural"),
        "english_jenny": ("english", "en-US-JennyNeural"), "jenny": ("english", "en-US-JennyNeural"),
        "english": ("english", "en-US-GuyNeural"), "en": ("english", "en-US-GuyNeural"),  # auto multi-voice
        **{shortcut: ("burmese", voice) for shortcut, voice in gemini_tts.SHORTCUTS.items()},
    }
    if tts_voice:
        return language, gemini_tts.canonical_voice(voice_aliases.get(tts_voice, tts_voice))
    return language_defaults.get(language, (language, None))


# =============================================================================
# SECTION 5: PIPELINE WORKER ENGINES
# (အဓိက ဗီဒီယို ထုတ်လုပ်မှု Worker များ - Recap, Subtitles, Hardsub, Batch)
# =============================================================================

def pipeline_worker(
    job_id,
    input_source,
    language="burmese",
    subtitle_mode="burn",
    resolution="1080p",
    tts_engine=None,
    custom_thumb_title=None,
    watermark_enabled=None,
    watermark_text=None,
    watermark_opacity=None,
    reels_enabled=True,
    video_format="both",
    subtitle_style="box_black",
    thumbnail_intro=False,
    source_language="auto",
    skip_demucs=False,
    detect_scenes=False,
    resume=True,
    tts_voice=None,
    script_engine="recap",
    trim_end=None,
    no_smart_trim=False,
    outro_card=False,
    translation_style="recap",
    audio_mode="ai_voiceover",
    sfx_mode="original_sfx",
    sfx_volume=0.15,
    blur_mode="auto",
    blur_height=None,
    mirror=False,
    audio_anti_copyright=False,
    render_video=True,
    stage_toggles=None,
    context_hint=None,
    story_length="standard",
    narrative_tone="natural",
    tts_rate=None,
    delivery_mode="oneclick",
    sync_mode="audio_fit",
):
    """
    Executes the Complete Movie Recap Pipeline via MasterAgent.
    ဇာတ်ကား အကျဉ်းချုပ် ဗီဒီယို အပြည့်အစုံကို MasterAgent ဖြင့် စတင်ထုတ်လုပ်ပေးသည့် Worker ဖြစ်ပါသည်။

    Pipeline Stages:
      1. Video & Audio Ingestion (YouTube URL auto-download or local file)
      2. Vocal Separation & Demucs Isolation
      3. Whisper Speech-to-Text Dialogue Extraction
      4. Gemini Vision Scene & Storyline Analysis
      5. Multi-lingual Script Synthesis (Burmese/English/Thai)
      6. AI Voiceover Narration (Edge-TTS / F5-TTS)
      7. Anti-Copyright Hardsubbing & Video Compositing
    """
    _apply_pipeline_env(skip_demucs, detect_scenes, video_format, reels_enabled)
    started_with = options_from_call(pipeline_worker, locals())   # remembered per project for "re-render"

    def work():
        if DownloaderAgent.is_url(input_source):
            _set_job_state(job_id, phase='Downloading Video...')
            print(f"[URL] Detected URL: {input_source} - starting auto-download...")
            movie_path = DownloaderAgent(output_dir="movies").download_video(input_source)
        else:
            _set_job_state(job_id, phase='Processing Local File...')
            movie_path = _resolve_input_source(input_source)

        clean_lang, tts_voice_override = _resolve_voice_mapping(language, tts_voice)

        master = MasterAgent(
            movie_path,
            language=clean_lang,
            subtitle_mode=subtitle_mode,
            subtitle_style=subtitle_style,
            resolution=resolution,
            tts_engine=tts_engine,
            tts_voice=tts_voice_override,
            custom_thumb_title=custom_thumb_title,
            watermark_enabled=watermark_enabled,
            watermark_text=watermark_text,
            watermark_opacity=watermark_opacity,
            video_format=video_format,
            thumbnail_intro=thumbnail_intro,
            source_language=source_language,
            script_engine=script_engine,
            resume=resume,
            trim_end=trim_end,
            no_smart_trim=no_smart_trim,
            outro_card=outro_card,
            cancel_event=cancel_events.get(job_id),
            skip_demucs=skip_demucs,
            detect_scenes=detect_scenes,
            translation_style=translation_style,
            audio_mode=audio_mode,
            sfx_mode=sfx_mode,
            sfx_volume=sfx_volume,
            blur_mode=blur_mode,
            blur_height=blur_height,
            mirror=mirror,
            audio_anti_copyright=audio_anti_copyright,
            render_video=render_video,
            stage_toggles=stage_toggles,
            context_hint=context_hint,
            story_length=story_length,
            narrative_tone=narrative_tone,
            tts_rate=tts_rate,
            delivery_mode=delivery_mode,
            sync_mode=sync_mode,
        )
        project_path = os.path.join(master.output_dir, master.state.project_dir)
        project_store.save_job_options(project_path, started_with)
        master.run_pipeline()
        project_store.write_quality_report(project_path, master.state.project_dir)   # shown in the History view
        package = (master.package_info or {}).get("zip")          # set when the delivery mode built a CapCut package
        return {"project_dir": master.state.project_dir, "package": package}

    _run_job(job_id, input_source, "Starting...", work)


def subtitle_worker(
    job_id,
    input_source,
    project_name=None,
    source_language="auto",
    force_whisper=False,
    translation_style="dialogue",
    audio_mode="original",
    sfx_mode="original_sfx",
    sfx_volume=0.15,
    render_video=False,
    video_format="16:9",
    resolution="1080p",
    subtitle_style="box_black",
    blur_mode="auto",
    mirror=False,
    color_grading=True,
    blur_height=None,
    audio_anti_copyright=False,
    stage_toggles=None,
    context_hint=None,
    delivery_mode="oneclick",
):
    """
    Executes the Subtitle Generation & Translation Engine.
    စာတန်းထိုး သီးသန့် ထုတ်လုပ်ခြင်းနှင့် ဘာသာပြန်ဆိုခြင်း လုပ်ငန်းများကို ဆောင်ရွက်ပေးသည့် Worker ဖြစ်ပါသည်။

    Capabilities:
      - Speech-to-Text transcription via OpenAI Whisper
      - Nuanced multilingual translation via Gemini AI
      - SRT, ASS subtitle export and optional hardsub rendering
      - Anti-copyright filters (mirror, blur, color grading)
    """
    def work():
        from subtitle_engine import SubtitleEngine
        engine = SubtitleEngine(output_base_dir="outputs", cancel_event=cancel_events.get(job_id))
        _set_job_state(job_id, phase='Processing Subtitles...')
        print(f"[*] Subtitle Engine: Starting job {job_id} for {input_source}...")
        return engine.run(
            input_source=input_source,
            project_name=project_name,
            source_language=source_language,
            force_whisper=force_whisper,
            translation_style=translation_style,
            audio_mode=audio_mode,
            sfx_mode=sfx_mode,
            sfx_volume=sfx_volume,
            render_video=render_video,
            video_format=video_format,
            resolution=resolution,
            subtitle_style=subtitle_style,
            blur_mode=blur_mode,
            mirror=mirror,
            color_grading=color_grading,
            blur_height=blur_height,
            audio_anti_copyright=audio_anti_copyright,
            stage_toggles=stage_toggles,
            context_hint=context_hint,
            delivery_mode=delivery_mode,
        )

    _run_job(job_id, input_source, "Starting Subtitle Engine...", work)


def hardsub_worker(
    job_id,
    input_source,
    project_name=None,
    source_language="auto",
    force_whisper=False,
    video_format="both",
    resolution="1080p",
    subtitle_style="box_black",
    blur_mode="auto",
    mirror=False,
    color_grading=True,
    blur_height=None,
    audio_anti_copyright=False,
    translation_style="persona",
    audio_mode="original",
    sfx_mode="original_sfx",
    sfx_volume=0.15,
    render_video=True,
    stage_toggles=None,
    context_hint=None,
    delivery_mode="oneclick",
):
    """
    Executes the Anti-Copyright Hardsub Video Compositing Engine.
    မူပိုင်ခွင့် အကာအကွယ် စစ်ထုတ်မှုများနှင့် စာတန်းထိုး ဗီဒီယို ပေါင်းစပ် ထုတ်လုပ်ပေးသည့် Worker ဖြစ်ပါသည်။

    Features:
      - Persona-level spoken dialogue translation
      - AI Subtitle Boxblur detection (Y-axis region masking)
      - Video zoom (1.02x), EQ color grading, mirroring
      - Audio anti-copyright frequency tempo shield (atempo=1.008)
      - Hardware-accelerated H.264 video encoding (Intel QSV, NVENC, Apple VideoToolbox, libx264)
    """
    def work():
        from hardsub_engine import HardsubEngine
        engine = HardsubEngine(output_base_dir="outputs", cancel_event=cancel_events[job_id])
        _set_job_state(job_id, phase='Processing Hardsub Video...')
        print(f"[*] Hardsub Studio: Starting job {job_id} for {input_source} (Format: {video_format}, Res: {resolution})...")
        return engine.run(
            input_source=input_source,
            project_name=project_name,
            source_language=source_language,
            force_whisper=force_whisper,
            video_format=video_format,
            resolution=resolution,
            subtitle_style=subtitle_style,
            blur_mode=blur_mode,
            blur_height=blur_height,
            mirror=mirror,
            color_grading=color_grading,
            audio_anti_copyright=audio_anti_copyright,
            translation_style=translation_style,
            audio_mode=audio_mode,
            sfx_mode=sfx_mode,
            sfx_volume=sfx_volume,
            render_video=render_video,
            stage_toggles=stage_toggles,
            context_hint=context_hint,
            delivery_mode=delivery_mode,
        )

    _run_job(job_id, input_source, "Starting Hardsub Studio...", work)


def batch_worker(
    job_id,
    inputs_list,
    language="burmese",
    subtitle_mode="burn",
    resolution="1080p",
    tts_engine=None,
    custom_thumb_title=None,
    watermark_enabled=None,
    watermark_text=None,
    watermark_opacity=None,
    reels_enabled=True,
    video_format="both",
    subtitle_style="box_black",
    thumbnail_intro=False,
    source_language="auto",
    skip_demucs=False,
    detect_scenes=False,
    resume=True,
    tts_voice=None,
    script_engine="recap",
    engine_mode="recap",
    blur_mode="auto",
    blur_height=None,
    mirror=False,
    color_grading=True,
    audio_anti_copyright=False,
    force_whisper=False,
    translation_style=None,
    audio_mode=None,
    sfx_mode="original_sfx",
    sfx_volume=0.15,
    render_video=True,
    stage_toggles=None,
    context_hint=None,
    story_length="standard",
    narrative_tone="natural",
    tts_rate=None,
    delivery_mode="oneclick",
    sync_mode="audio_fit",
):
    """
    Executes Multi-Video Batch Processing across any of the 3 production engines.
    ဗီဒီယို အများအပြားကို တစ်ပြိုင်နက် သို့မဟုတ် အစဉ်လိုက် အလိုအလျောက် ထုတ်လုပ်ပေးသည့် Batch Worker ဖြစ်ပါသည်။

    Supported Engine Modes:
      - 'recap': Full movie recap workflow via BatchProcessor
      - 'subtitle': Whisper transcription and translation batch
      - 'hardsub': Anti-copyright subtitle compositing batch
    """
    from brain.planner import BatchProcessor
    _apply_pipeline_env(skip_demucs, detect_scenes, video_format, reels_enabled)
    started_with = options_from_call(batch_worker, locals())   # remembered per project for "re-render"
    started_with.pop("inputs", None)

    def _run_item_engine(label: str, engine, run_item):
        """Runs an engine over every input, honouring Stop between items."""
        total_items = len(inputs_list)
        print(f"[*] Batch {label}: Starting batch of {total_items} items...")
        for idx, item in enumerate(inputs_list, 1):
            if _is_job_cancelled(job_id):
                break
            _set_job_state(job_id, phase=f"{label} Item {idx}/{total_items}: {os.path.basename(item)[:30]}...")
            print(f"\n{'='*65}\n[BATCH {label.upper()}] Item {idx}/{total_items}: {item}\n{'='*65}")
            try:
                run_item(engine, item)
            except Exception as item_err:
                print(f"[ERROR] Batch item {idx} failed: {item_err}")

    def remember_project(stage, master, movie_path):
        """Per recap project: save the settings it was started with, then its quality report (History view)."""
        project_path = os.path.join(master.output_dir, master.state.project_dir)
        if stage == "start":
            project_store.save_job_options(project_path, dict(started_with, input=movie_path))
        else:
            project_store.write_quality_report(project_path, master.state.project_dir)

    def work():
        if engine_mode == "hardsub":
            from hardsub_engine import HardsubEngine
            engine = HardsubEngine(output_base_dir="outputs", cancel_event=cancel_events.get(job_id))
            _run_item_engine("Hardsub Studio", engine, lambda eng, item: eng.run(
                input_source=item,
                source_language=source_language or "auto",
                force_whisper=force_whisper,
                video_format=video_format or "both",
                resolution=resolution or "1080p",
                subtitle_style=subtitle_style or "box_black",
                blur_mode=blur_mode or "auto",
                blur_height=blur_height,
                mirror=mirror,
                color_grading=color_grading,
                audio_anti_copyright=audio_anti_copyright,
                translation_style=translation_style or "persona",
                audio_mode=audio_mode or "original",
                sfx_mode=sfx_mode or "original_sfx",
                sfx_volume=sfx_volume,
                render_video=render_video if render_video is not None else True,
                stage_toggles=stage_toggles,
                context_hint=context_hint,
                delivery_mode=delivery_mode,
            ))
        elif engine_mode == "subtitle":
            from subtitle_engine import SubtitleEngine
            engine = SubtitleEngine(output_base_dir="outputs", cancel_event=cancel_events.get(job_id))
            _run_item_engine("Subtitle Engine", engine, lambda eng, item: eng.run(
                input_source=item,
                source_language=source_language or "auto",
                force_whisper=force_whisper,
                translation_style=translation_style or "dialogue",
                audio_mode=audio_mode or "original",
                sfx_mode=sfx_mode or "original_sfx",
                sfx_volume=sfx_volume,
                render_video=render_video if render_video is not None else False,
                video_format=video_format or "16:9",
                resolution=resolution or "1080p",
                subtitle_style=subtitle_style or "box_black",
                blur_mode=blur_mode or "auto",
                mirror=mirror,
                color_grading=color_grading,
                blur_height=blur_height,
                audio_anti_copyright=audio_anti_copyright,
                stage_toggles=stage_toggles,
                context_hint=context_hint,
                delivery_mode=delivery_mode,
            ))
        else:
            urls = [i for i in inputs_list if DownloaderAgent.is_url(i)]
            local_paths = [_resolve_input_source(i) for i in inputs_list if not DownloaderAgent.is_url(i)]
            clean_lang, tts_voice_override = _resolve_voice_mapping(language, tts_voice)

            processor = BatchProcessor(
                movies_folder="movies",
                skip_completed=True,
                language=clean_lang,
                subtitle_mode=subtitle_mode,
                subtitle_style=subtitle_style,
                resolution=resolution,
                tts_engine=tts_engine,
                tts_voice=tts_voice_override,
                custom_thumb_title=custom_thumb_title,
                watermark_enabled=watermark_enabled,
                watermark_text=watermark_text,
                watermark_opacity=watermark_opacity,
                video_format=video_format,
                thumbnail_intro=thumbnail_intro,
                source_language=source_language,
                script_engine=script_engine,
                resume=resume,
                cancel_event=cancel_events.get(job_id),
                skip_demucs=skip_demucs,
                detect_scenes=detect_scenes,
                translation_style=translation_style or "recap",
                audio_mode=audio_mode or "ai_voiceover",
                sfx_mode=sfx_mode or "original_sfx",
                sfx_volume=sfx_volume,
                blur_mode=blur_mode,
                blur_height=blur_height,
                mirror=mirror,
                audio_anti_copyright=audio_anti_copyright,
                render_video=render_video if render_video is not None else True,
                stage_toggles=stage_toggles,
                context_hint=context_hint,
                story_length=story_length,
                narrative_tone=narrative_tone,
                tts_rate=tts_rate,
                on_project=remember_project,
                delivery_mode=delivery_mode,
                sync_mode=sync_mode,
            )
            print(f"[*] Batch Mode: Starting batch run for {len(inputs_list)} item(s)...")
            processor.process_all(url_list=urls, local_paths=local_paths)

    _run_job(job_id, f"Batch ({len(inputs_list)} items)", "Starting...", work)


def downloader_worker(job_id, url, resolution="1080p", custom_filename=None):
    """
    Executes a dedicated video download job in the background.
    ဗီဒီယို ဖိုင်ကို movies/ ဖိုဒါထဲသို့ သီးသန့် ဒေါင်းလုဒ်ဆွဲပေးသည့် Worker ဖြစ်ပါသည်။
    """
    def work():
        _set_job_state(job_id, phase=f'Downloading Video ({resolution})...')
        print(f"[*] Downloader: Starting dedicated download for {url} (Res: {resolution})...")
        downloaded = DownloaderAgent(output_dir="movies").download_video(
            url, resolution=resolution, custom_filename=custom_filename
        )
        print(f"[OK] Downloader: Video saved successfully -> {downloaded}")
        return {"phase": f"Downloaded: {os.path.basename(downloaded)}"}

    _run_job(job_id, url, "Downloading Video...", work, db_status="running")


def splitter_worker(
    job_id,
    input_source,
    project_name=None,
    split_mode="duration",
    split_minutes=10.0,
    split_parts=2,
    split_snap=True,
    split_reencode=False,
    resolution="1080p",
    split_format="original",
    split_fill="blur",
    split_label="",
):
    """
    Executes the Splitter tool: cuts a long video into parts.
    ရှည်လျားသော ဗီဒီယိုကို အပိုင်းများအဖြစ် ဖြတ်ပေးသည့် Worker ဖြစ်ပါသည်။
    """
    def work():
        from splitter_engine import SplitterEngine
        engine = SplitterEngine(output_base_dir="outputs", cancel_event=cancel_events.get(job_id))
        _set_job_state(job_id, phase='Splitting video...')
        print(f"[*] Splitter: Starting job {job_id} for {input_source}...")
        return engine.run(
            input_source=input_source,
            project_name=project_name,
            split_mode=split_mode,
            split_minutes=split_minutes,
            split_parts=split_parts,
            split_snap=split_snap,
            split_reencode=split_reencode,
            resolution=resolution,
            split_format=split_format,
            split_fill=split_fill,
            split_label=split_label,
        )

    _run_job(job_id, input_source, "Starting Splitter...", work)


def shorts_worker(
    job_id,
    input_source,
    project_name=None,
    source_language="auto",
    clip_count=5,
    clip_min_sec=20.0,
    clip_max_sec=60.0,
    clip_focus="balanced",
    short_subtitles="burmese",
    short_hook_title=True,
    short_framing="fit",
    translation_style="dialogue",
    subtitle_style="box_black",
    video_format="9:16",
    resolution="1080p",
    context_hint=None,
    delivery_mode="oneclick",
):
    """
    Executes the Long to Short tool: picks the best moments of a long video and renders them as Shorts.
    ဗီဒီယိုရှည်ထဲက အကောင်းဆုံး အပိုင်းများကို Shorts / Reels အဖြစ် ထုတ်ပေးသည့် Worker ဖြစ်ပါသည်။
    """
    def work():
        from shorts_engine import ShortsEngine
        engine = ShortsEngine(output_base_dir="outputs", cancel_event=cancel_events.get(job_id))
        _set_job_state(job_id, phase='Making Shorts...')
        print(f"[*] Shorts: Starting job {job_id} for {input_source}...")
        return engine.run(
            input_source=input_source,
            project_name=project_name,
            source_language=source_language,
            clip_count=clip_count,
            clip_min_sec=clip_min_sec,
            clip_max_sec=clip_max_sec,
            clip_focus=clip_focus,
            short_subtitles=short_subtitles,
            short_hook_title=short_hook_title,
            short_framing=short_framing,
            translation_style=translation_style,
            subtitle_style=subtitle_style,
            video_format=video_format,
            resolution=resolution,
            context_hint=context_hint,
            delivery_mode=delivery_mode,
        )

    _run_job(job_id, input_source, "Starting Shorts...", work)
