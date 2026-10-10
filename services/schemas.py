"""
Request Schemas (API Request Model များ)
=======================================
Pydantic models validating every JSON body accepted by the dashboard API.

Dashboard API သို့ ပေးပို့သော JSON data အားလုံးကို စစ်ဆေးသည့် Pydantic model များ။
"""

from typing import Dict, List, Optional

from pydantic import BaseModel


class StartRequest(BaseModel):
    """
    Schema for initiating a single video production pipeline job.
    ဗီဒီယို တစ်ခုချင်းစီ ထုတ်လုပ်ရန် API သို့ ပေးပို့ရမည့် အချက်အလက်များ ပုံစံခွက်။
    """
    input: str
    engine_mode: Optional[str] = "recap"
    project_name: Optional[str] = None
    force_whisper: Optional[bool] = False
    language: Optional[str] = "burmese"
    subtitle_mode: Optional[str] = "burn"
    resolution: Optional[str] = "1080p"
    tts_engine: Optional[str] = None
    tts_voice: Optional[str] = None
    custom_thumb_title: Optional[str] = None
    watermark_enabled: Optional[bool] = True
    watermark_text: Optional[str] = None
    watermark_opacity: Optional[float] = None
    reels_enabled: Optional[bool] = True
    video_format: Optional[str] = "both"
    subtitle_style: Optional[str] = "box_black"
    thumbnail_intro: Optional[bool] = False
    source_language: Optional[str] = "auto"
    skip_demucs: Optional[bool] = False
    detect_scenes: Optional[bool] = False
    script_engine: Optional[str] = "recap"
    resume: Optional[bool] = True
    trim_end: Optional[float] = None
    no_smart_trim: Optional[bool] = False
    outro_card: Optional[bool] = False
    blur_mode: Optional[str] = "auto"
    blur_height: Optional[float] = None
    mirror: Optional[bool] = False
    color_grading: Optional[bool] = True
    audio_anti_copyright: Optional[bool] = False
    translation_style: Optional[str] = "recap"
    audio_mode: Optional[str] = "ai_voiceover"
    sfx_mode: Optional[str] = "original_sfx"
    sfx_volume: Optional[float] = 0.15
    render_video: Optional[bool] = True
    stage_toggles: Optional[Dict[str, bool]] = None
    context_hint: Optional[str] = None


class BatchStartRequest(BaseModel):
    """
    Schema for initiating multi-video batch execution.
    ဗီဒီယို အများအပြားကို အလိုအလျောက် ဆက်တိုက် ထုတ်လုပ်ရန် ပေးပို့ရမည့် ပုံစံခွက်။
    """
    inputs: List[str]
    engine_mode: Optional[str] = "recap"
    force_whisper: Optional[bool] = False
    language: Optional[str] = "burmese"
    subtitle_mode: Optional[str] = "burn"
    resolution: Optional[str] = "1080p"
    tts_engine: Optional[str] = None
    tts_voice: Optional[str] = None
    custom_thumb_title: Optional[str] = None
    watermark_enabled: Optional[bool] = True
    watermark_text: Optional[str] = None
    watermark_opacity: Optional[float] = None
    reels_enabled: Optional[bool] = True
    video_format: Optional[str] = "both"
    subtitle_style: Optional[str] = "box_black"
    thumbnail_intro: Optional[bool] = False
    source_language: Optional[str] = "auto"
    skip_demucs: Optional[bool] = False
    detect_scenes: Optional[bool] = False
    script_engine: Optional[str] = "recap"
    resume: Optional[bool] = True
    blur_mode: Optional[str] = "auto"
    blur_height: Optional[float] = None
    mirror: Optional[bool] = False
    color_grading: Optional[bool] = True
    audio_anti_copyright: Optional[bool] = False
    translation_style: Optional[str] = "recap"
    audio_mode: Optional[str] = "ai_voiceover"
    sfx_mode: Optional[str] = "original_sfx"
    sfx_volume: Optional[float] = 0.15
    render_video: Optional[bool] = True
    stage_toggles: Optional[Dict[str, bool]] = None
    context_hint: Optional[str] = None


class SubtitleConfigRequest(BaseModel):
    """Schema for updating default subtitle style presets."""
    preset: str = "box_black"


class BrandingConfigRequest(BaseModel):
    """Schema for updating watermark overlay parameters."""
    watermark_enabled: bool = True
    watermark_text: str = "Pai Ai Movie Studio"
    watermark_opacity: float = 0.4
    watermark_margin: int = 30
    watermark_font_size: int = 40


class RenameRequest(BaseModel):
    """Schema for renaming movie projects in storage."""
    old_name: str
    new_name: str


class SaveKeysRequest(BaseModel):
    """Schema for updating and reconciling Gemini API keys."""
    keys: List[str]


class CookieSaveRequest(BaseModel):
    """Schema for uploading Netscape format cookies text."""
    content: str


class DownloadVideoRequest(BaseModel):
    """Schema for dedicated video download request from URL."""
    url: str
    resolution: Optional[str] = "1080p"
    custom_name: Optional[str] = None
