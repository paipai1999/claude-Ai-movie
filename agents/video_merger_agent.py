"""
Video Merger Agent (ဗီဒီယိုပေါင်းစပ်ရေး Agent)
============================================
Public entry point of the final render phase. The implementation lives in the
`agents/merger/` package, split by responsibility:

  encoder.py        FFmpeg discovery + hardware encoder selection
  media_utils.py    duration probing, audio-stream detection, voiceover track assembly
  intro_outro.py    thumbnail intro, outro card, watermark image
  subtitles.py      ASS generation, Burmese wrapping, SRT/ASS export
  vision_blur.py    Vision AI subtitle detection + boxblur pass
  reels.py          9:16 Reels canvas export
  legacy_moviepy.py slower MoviePy fallback compositor
  plan.py           MergePlan dataclass carrying every render decision
  voiceover.py      clip discovery, scene-anchored timeline, voiceover track
  background_audio.py  original / Demucs / BGM bed selection
  smart_sync.py     Smart Sync: the re-timed film (the picture follows the voice)
  single_pass.py    filtergraph + one-pass ffmpeg render + intro/outro

Existing imports such as `from agents.video_merger_agent import detect_hardware_encoder`
keep working (names are re-exported below).

နောက်ဆုံး ဗီဒီယိုထုတ်လုပ်ခြင်း Phase ၏ အဓိက class ဖြစ်သည်။ အသေးစိတ်လုပ်ဆောင်ချက်များကို
`agents/merger/` ဖိုဒါအောက်တွင် တာဝန်အလိုက် ခွဲထားသည်။
"""

import os
import sys

import brain.config as cfg

# Force UTF-8 output on Windows to prevent emoji/Unicode encode errors
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from agents.merger.encoder import (  # noqa: E402  (re-exported public helpers)
    _ensure_linux_cuda_ld_path,
    _auto_setup_nvenc_linux,
    _get_ffmpeg_bin,
    detect_hardware_encoder,
)
from agents.merger.media_utils import (  # noqa: E402
    _get_safe_ascii_id,
    _get_audio_duration,
    _get_video_info,
    _has_audio_stream,
    _assemble_voiceover_track,
)
from agents.merger.intro_outro import IntroOutroMixin  # noqa: E402
from agents.merger.legacy_moviepy import LegacyMoviepyMixin  # noqa: E402
from agents.merger.subtitles import SubtitleOverlayMixin  # noqa: E402
from agents.merger.vision_blur import SubtitleBlurMixin  # noqa: E402
from agents.merger.reels import ReelsMixin  # noqa: E402
from agents.merger.voiceover import VoiceoverLayoutMixin  # noqa: E402
from agents.merger.background_audio import BackgroundAudioMixin  # noqa: E402
from agents.merger.single_pass import SinglePassMixin  # noqa: E402
from agents.merger.smart_sync import SmartSyncMixin  # noqa: E402
from agents.merger.plan import MergePlan  # noqa: E402

__all__ = [
    "VideoMergerAgent",
    "detect_hardware_encoder",
    "_get_ffmpeg_bin",
    "_get_safe_ascii_id",
    "_get_audio_duration",
    "_get_video_info",
    "_has_audio_stream",
    "_assemble_voiceover_track",
    "_ensure_linux_cuda_ld_path",
    "_auto_setup_nvenc_linux",
]


class VideoMergerAgent(
    VoiceoverLayoutMixin,
    SmartSyncMixin,
    BackgroundAudioMixin,
    SinglePassMixin,
    IntroOutroMixin,
    LegacyMoviepyMixin,
    SubtitleOverlayMixin,
    SubtitleBlurMixin,
    ReelsMixin,
):
    def __init__(
        self,
        output_dir: str = "outputs",
        subtitle_blur_override: bool = None,
        subtitle_mode: str = "burn",
        resolution: str = "1080p",
    ):
        self.output_dir = output_dir
        self.subtitle_blur_override = subtitle_blur_override
        self.subtitle_mode = str(subtitle_mode or "burn").lower()
        self.resolution = str(resolution or "1080p").lower()

    def _build_merge_plan(self, state, movie_path: str) -> MergePlan:
        """Runs every preparation step (probe, voiceover layout, audio bed, subtitles ...) in order."""
        plan = MergePlan()
        plan.output_dir = os.path.join(self.output_dir, state.project_dir)
        os.makedirs(plan.output_dir, exist_ok=True)
        plan.final_output = os.path.join(plan.output_dir, "final_recap.mp4")

        plan.config_data = config_data = cfg.load_config()
        plan.copyright_cfg = config_data.get("copyright_protection", {})
        plan.copyright_enabled = plan.copyright_cfg.get("enabled", True)
        blur_cfg = config_data.get("subtitle_blur", {})
        plan.blur_enabled = blur_cfg.get("enabled", True)
        if self.subtitle_blur_override:
            plan.blur_enabled = True
        plan.blur_strength = int(blur_cfg.get("blur_strength", 18))            # boxblur radius
        color_cfg = config_data.get("color_grading", {})
        plan.color_enabled = color_cfg.get("enabled", True)
        plan.cg_brightness = float(color_cfg.get("brightness", 0.03))
        plan.cg_contrast = float(color_cfg.get("contrast", 1.02))
        plan.cg_saturation = float(color_cfg.get("saturation", 1.08))

        print(f"[*] VideoMerger: Starting video merge process (Copyright-Safe Mode: {plan.copyright_enabled})...")

        # ── 1. Fast Video Metadata Extraction (10ms, Zero RAM) ───────────────
        v_info = _get_video_info(movie_path)
        plan.video_dur = v_info.get("duration", 0.0)
        plan.video_w = v_info.get("width", 1920)
        plan.video_h = v_info.get("height", 1080)
        plan.video_fps = v_info.get("fps", 24.0)
        print(f"[*] VideoMerger: Loaded video stream metadata ({plan.video_dur:.1f}s, size: {plan.video_w}x{plan.video_h}, fps: {plan.video_fps:.1f}).")

        # ── 2 - 3b. Voiceover clips, scene-anchored timeline, output length ──
        self._collect_voiceover_clips(plan, state)
        plan.temp_dir = os.path.abspath("temp")
        os.makedirs(plan.temp_dir, exist_ok=True)
        plan.safe_id = _get_safe_ascii_id(movie_path)          # of the ORIGINAL film: a re-timed copy would be named the same everywhere
        plan.movie_path = movie_path
        movie_path = self._apply_smart_sync(plan, state, movie_path)        # "smart": the picture follows the voice
        try:
            return self._finish_merge_plan(plan, state, movie_path)
        except BaseException:
            self._cleanup_smart_sync(plan)                       # a failed merge leaves no re-timed film behind
            raise

    def _finish_merge_plan(self, plan: MergePlan, state, movie_path: str) -> MergePlan:
        """Steps 3 - 8: the lines on the (possibly re-timed) film, the voiceover track, background audio, subtitles ..."""
        self._layout_voiceover_clips(plan, state)
        self._resolve_render_duration(plan, state)

        # ── 4. Linear PCM voiceover track ─────────────────────────────────────
        plan.persistent_clean_path = os.path.join(
            plan.output_dir, f"{os.path.splitext(os.path.basename(plan.final_output))[0]}_clean.mp4"
        )
        plan.clean_video_path = os.path.join(plan.temp_dir, f"{plan.safe_id}_clean.mp4")
        plan.assembled_vo_path = os.path.join(plan.temp_dir, f"{plan.safe_id}_vo_track.wav")
        self._assemble_voiceover(plan)

        # ── 5 - 8. Background audio, subtitles, watermark, intro, blur ───────
        self._select_background_audio(plan, state, movie_path)
        self._retime_background(plan)
        self._prepare_subtitles(plan, state)
        self._prepare_watermark(plan, state)
        self._plan_thumbnail_intro(plan, state)
        self._plan_subtitle_blur(plan, state, movie_path)
        return plan

    def merge_video(self, state, movie_path: str):
        """
        Final render: prepare a MergePlan, render it with one FFmpeg pass, add intro/outro cards.
        If the FFmpeg pass cannot produce a video the slower MoviePy compositor is used, and if that
        also fails the job fails loudly instead of "completing" without a video.

        FFmpeg single-pass မအောင်မြင်ပါက MoviePy fallback ကို သုံးပြီး၊ ဗီဒီယိုမထွက်ပါက error ပေးသည်။
        """
        plan = self._build_merge_plan(state, movie_path)
        try:
            movie_path = plan.movie_path or movie_path                          # the re-timed copy under Smart Sync

            # ── 9. Pure FFmpeg Single-Pass Video & Audio Compositing ──────────────
            if self._render_single_pass(plan, state, movie_path):
                self._apply_intro_outro(plan, state)
                return state

            if plan.sync_plan is not None:
                # The MoviePy compositor works from the script's own times, which belong to the ORIGINAL film.
                raise RuntimeError(
                    "The FFmpeg render failed on the Smart Sync film and the slower MoviePy fallback cannot follow the "
                    "re-timed picture. Check the FFmpeg log above, or run again with the sync mode 'audio_fit' (--sync-mode audio_fit).")

            # ── 10. Fallback Legacy Path (Only if Single-Pass failed) ──
            print("[WARN] VideoMerger: Single-pass FFmpeg render produced no video. Falling back to the legacy MoviePy compositor...")
            merged_state = self._legacy_moviepy_merge(
                state, movie_path, plan.output_dir, plan.final_output, plan.persistent_clean_path, plan.clean_video_path,
                plan.config_data, plan.burn_subs, plan.target_ass_path, plan.subtitle_timings
            )
            if not (os.path.exists(plan.final_output) and os.path.getsize(plan.final_output) > 1000):
                raise RuntimeError(
                    "Video rendering failed: neither the FFmpeg single-pass nor the MoviePy fallback produced "
                    f"'{os.path.basename(plan.final_output)}'. Check the FFmpeg log above. "
                    "ဗီဒီယို render မအောင်မြင်ပါ - အထက်ပါ FFmpeg log ကိုစစ်ပါ။"
                )
            return merged_state
        finally:
            self._cleanup_smart_sync(plan)
