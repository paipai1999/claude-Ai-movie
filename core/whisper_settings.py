"""
Whisper Speech-to-Text Settings (Whisper ဆက်တင်များ - Engine ၃ ခုလုံး တူညီစွာသုံးရန်)
====================================================================================
One place that decides WHICH Whisper model, device, precision and beam size every engine uses,
so transcript quality no longer depends on which studio engine happened to run.

  pipeline.whisper_model      : "auto" | tiny | base | small | medium | large-v3 ...
                                "auto" -> "medium" on a CUDA GPU, "base" on CPU.
  pipeline.whisper_beam_size  : 1 (fastest, greedy) ... 5 (noticeably more accurate)

ဤ module သည် Whisper model / device / beam size ကို Engine ၁၊ ၂၊ ၃ အားလုံး အတူတူ ဆုံးဖြတ်ပေးသည်။
"auto" ဆိုပါက GPU ရှိလျှင် `medium`၊ မရှိလျှင် `base` ကို အလိုအလျောက် ရွေးသည်။
"""

import os
from typing import Any, Dict, Optional, Tuple

DEFAULT_CPU_MODEL = "base"
DEFAULT_GPU_MODEL = "medium"


def cuda_available() -> bool:
    """True when PyTorch can see a CUDA GPU (False when torch is missing or broken)."""
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def resolve_whisper_model(configured: Optional[str], cuda: Optional[bool] = None) -> str:
    """Turns the configured model name ("auto" allowed) into a concrete Whisper model name."""
    name = str(configured or "").strip().lower()
    if name in ("", "auto"):
        has_cuda = cuda_available() if cuda is None else cuda
        return DEFAULT_GPU_MODEL if has_cuda else DEFAULT_CPU_MODEL
    return name


def resolve_beam_size(pipeline_cfg: Optional[Dict[str, Any]]) -> int:
    """Beam size from config (>= 1). WHISPER_BEAM_SIZE env var overrides the file."""
    raw = os.getenv("WHISPER_BEAM_SIZE") or (pipeline_cfg or {}).get("whisper_beam_size", 1)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def runtime_for(cuda: Optional[bool] = None) -> Tuple[str, str]:
    """(device, compute_type): GPU float16 when CUDA exists, otherwise CPU int8."""
    has_cuda = cuda_available() if cuda is None else cuda
    return ("cuda", "float16") if has_cuda else ("cpu", "int8")


def load_faster_whisper(configured_model: Optional[str]):
    """
    Builds a faster-whisper WhisperModel using the shared policy and returns (model, model_name, device).
    A GPU that fails to initialise (missing cuDNN, out of memory, ...) falls back to CPU int8
    instead of aborting the whole job.

    GPU မစတင်နိုင်ပါက CPU int8 သို့ အလိုအလျောက် ပြန်ကျသည်။
    """
    from faster_whisper import WhisperModel

    cuda = cuda_available()
    model_name = resolve_whisper_model(configured_model, cuda)
    threads = min(8, os.cpu_count() or 4)
    device, compute = runtime_for(cuda)
    try:
        return WhisperModel(model_name, device=device, compute_type=compute, cpu_threads=threads), model_name, device
    except Exception as gpu_err:
        if device == "cpu":
            raise
        print(f"[WARN] Whisper GPU init failed ({gpu_err}); falling back to CPU int8.")
        cpu_model = resolve_whisper_model(configured_model, False)
        return WhisperModel(cpu_model, device="cpu", compute_type="int8", cpu_threads=threads), cpu_model, "cpu"
