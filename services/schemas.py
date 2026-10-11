"""
Request Schemas (API Request Model များ)
=======================================
Pydantic models validating every JSON body accepted by the dashboard API.

Dashboard API သို့ ပေးပို့သော JSON data အားလုံးကို စစ်ဆေးသည့် Pydantic model များ။
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, create_model

from core.options import OPTIONS


def _build_request_model(name: str, doc: str, *, batch: bool):
    """
    Generates the start-request schema from ``core.options`` so the API, CLI and wizard can never
    drift apart. Options that have per-engine defaults are ``None`` here and resolved per engine by
    ``core.options.resolve`` (so "not chosen" is distinguishable from "chose recap").
    """
    fields = {}
    if batch:
        fields["inputs"] = (List[str], ...)
    for opt in OPTIONS:
        if batch and not opt.batch:
            continue
        if opt.required:
            if not batch:
                fields[opt.name] = (str, ...)
            continue
        annotation = Dict[str, bool] if opt.type is dict else opt.type
        default = None if opt.engine_defaults else opt.default
        fields[opt.name] = (Optional[annotation], default)
    return create_model(name, __doc__=doc, __module__=__name__, **fields)


StartRequest = _build_request_model(
    "StartRequest",
    "Schema for initiating a single video production job (generated from core.options).\n"
    "ဗီဒီယို တစ်ခုချင်းစီ ထုတ်လုပ်ရန် API သို့ ပေးပို့ရမည့် အချက်အလက်များ ပုံစံခွက်။",
    batch=False,
)

BatchStartRequest = _build_request_model(
    "BatchStartRequest",
    "Schema for initiating multi-video batch execution (generated from core.options).\n"
    "ဗီဒီယို အများအပြားကို အလိုအလျောက် ဆက်တိုက် ထုတ်လုပ်ရန် ပေးပို့ရမည့် ပုံစံခွက်။",
    batch=True,
)


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
