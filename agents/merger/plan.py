"""
Merge Plan (ဗီဒီယိုပေါင်းစပ်ရန် စီမံချက်)
========================================
Plain data object that carries every decision made while preparing a final render (paths, timing,
audio source, subtitles, watermark, blur ...). `VideoMergerAgent.merge_video()` first FILLS the plan
step by step, then hands it to the renderer – so each step is small, named and individually testable.

Render မလုပ်မီ လိုအပ်သော ဆုံးဖြတ်ချက်အားလုံး (လမ်းကြောင်း၊ အချိန်၊ နောက်ခံအသံ၊ စာတန်း၊ watermark၊ blur)
ကို ဤ object တစ်ခုတည်းတွင် စုထားသည်။
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class MergePlan:
    # ── paths ────────────────────────────────────────────────────────────────
    output_dir: str = ""
    final_output: str = ""
    temp_dir: str = ""
    safe_id: str = ""
    persistent_clean_path: str = ""
    clean_video_path: str = ""
    assembled_vo_path: str = ""

    # ── configuration snapshot ───────────────────────────────────────────────
    config_data: Dict[str, Any] = field(default_factory=dict)
    copyright_cfg: Dict[str, Any] = field(default_factory=dict)
    copyright_enabled: bool = True
    blur_enabled: bool = True
    blur_strength: int = 18
    color_enabled: bool = True
    cg_brightness: float = 0.03
    cg_contrast: float = 1.02
    cg_saturation: float = 1.08

    # ── source video ─────────────────────────────────────────────────────────
    video_dur: float = 0.0
    video_w: int = 1920
    video_h: int = 1080
    video_fps: float = 24.0

    # ── Smart Sync (the picture follows the voice) ───────────────────────────
    movie_path: str = ""            # the film to merge: the source, or its re-timed copy
    sync_plan: Any = None           # a core.smart_sync.SyncPlan when the film was re-timed
    sync_work_dir: str = ""         # where the re-timed film and its segments are made (removed after the merge)

    # ── voiceover clips & timing ─────────────────────────────────────────────
    script_blocks: List[Any] = field(default_factory=list)
    audio_items: List[Tuple[int, str, float, Any]] = field(default_factory=list)
    clips_with_timing: List[Tuple[str, float, float]] = field(default_factory=list)
    subtitle_timings: List[Tuple[float, float, str]] = field(default_factory=list)
    curr_t: float = 0.0
    has_voiceover: bool = False

    # ── output length / outro handling ───────────────────────────────────────
    effective_video_dur: float = 0.0
    anti_sub_zoom_enabled: bool = True
    has_outro_card: bool = False

    # ── background audio ─────────────────────────────────────────────────────
    bg_source_type: str = "none"
    bg_audio_file: Optional[str] = None
    bg_extra_file: Optional[str] = None

    # ── subtitles ────────────────────────────────────────────────────────────
    burn_subs: bool = True
    target_ass_path: Optional[str] = None

    # ── watermark ────────────────────────────────────────────────────────────
    wm_png: Optional[str] = None
    wm_pos: str = "bottom_left"
    wm_margin: int = 25

    # ── thumbnail intro ──────────────────────────────────────────────────────
    has_thumb_intro: bool = False
    thumb_duration: float = 3.0
    thumbnail_path: str = ""

    # ── subtitle blur ────────────────────────────────────────────────────────
    do_blur: bool = False
    start_y_pct: float = 0.82
    height_pct: float = 0.18
    subtitle_found: bool = False
