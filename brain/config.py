from brain.diagnostics import log_suppressed
import json
import os
import copy

# Default configuration for the entire pipeline
DEFAULT_CONFIG = {
    "pipeline": {
        "language": "burmese",            # burmese (မြန်မာ - Thiha Voice) | english (အင်္ဂလိပ် - Guy Voice)
        "script_engine": "recap",         # "recap" (Natural Storyteller) | "translate" (Direct 1:1)
        "video_format": "both",            # "both" (16:9 + 9:16) | "16:9" (YouTube Landscape) | "9:16" (Facebook Reels / TikTok)
        "whisper_model": "auto",          # auto | tiny | base | small | medium | large-v3   ("auto" = medium on a CUDA GPU, base on CPU)
        "whisper_beam_size": 1,           # 1 = fastest (greedy). 5 = noticeably more accurate transcripts, ~2-3x slower
        "scene_threshold": 30.0,           # PySceneDetect sensitivity (lower = more scenes)
        "max_characters": 6,               # Max character names to detect
        "max_scenes_for_llm": 30,          # Scene count cap passed to LLM (context limit)
        "parallel_processing": True,       # Run STT & Scene detection in parallel (set False for Low RAM)
        "use_demucs": False,               # Vocal separation is opt-in because it is expensive on local machines
        "scene_detection": False           # Skip PySceneDetect in 1:1 dialogue mode (saves 10-25 mins per video)
    },
    "gemini": {
        "enabled": True,                   # Use Gemini API when writing Burmese recap scripts for natural spoken flow
        "api_keys": [],
        "model": "gemini-3.1-flash-lite",       # Primary model: gemini-3.1-flash-lite (ultra-fast ~1.3s verified model)
        "daily_limit_per_key": 1500,
        "model_limits": {
            "gemini-3.1-flash-lite": 1000,
            "gemini-3.6-flash": 1000,
            "gemini-flash-latest": 500,
            "gemini-flash-lite-latest": 1000,
            "gemini-3.7-flash": 500,
            "gemini-3.8-flash": 500,
            "gemini-3.5-flash-lite": 1000,
            "gemini-3.5-flash": 500
        },
        "models": {
            "heavy": "gemini-3.6-flash",
            "workhorse": "gemini-3.1-flash-lite",
            "polish": "gemini-3.1-flash-lite"
        }
    },
    "voice": {
        "enabled": True,
        "engine": "edge_tts",                 # "edge_tts" (Fast $0 Cloud TTS) | "f5_tts" (Zero-Shot Voice Cloning)
        "tts_voice_mm": "my-MM-ThihaNeural",  # Burmese voice (Thiha)
        "tts_voice_en": "en-US-GuyNeural",    # English voice (Guy)
        "tts_voice": "my-MM-ThihaNeural",     # Active default voice
        "tts_rate_mm": "+18%",
        "tts_rate_en": "+15%",
        "trim_silence": True,                 # cut Edge TTS lead-in/tail silence so lines start on time
        "gemini_tts": {                       # Gemini narrator: pick it (gemini:Kore) or let it stand in when Edge TTS is down
            "fallback": True,                 # False = never switch to Gemini on its own
            "fallback_voice": "",             # "" = Charon for a male Edge voice, Kore for a female one
            "models": [],                     # [] = the TTS models the account lists (or GEMINI_TTS_MODEL)
            "style": ""                       # "" = a calm, warm storyteller at the job's pace
        },
        "f5_tts": {
            "model_type": "F5-TTS",           # "F5-TTS" | "E2-TTS"
            "auto_character_cloning": True,   # Auto-extract reference audio slices for characters from Demucs vocals
            "default_ref_audio": "assets/voices/default_ref.wav",
            "default_ref_text": "",
            "speed": 1.0,
            "device": "auto"                  # "auto" | "cuda" | "cpu"
        }
    },
    "batch": {
        "movies_folder": "movies",
        "max_parallel_jobs": 1,            # Keep at 1 for CPU-only machines
        "skip_completed": True             # Skip movies with existing state.json output
    },
    "paths": {
        "output_dir": "outputs",
        "temp_dir": "temp",
        "clean_temp_after_merge": True
    },
    "copyright_protection": {
        "enabled": True,
        "mirror_video": False,
        "resize_factor": 1.02
    },
    "subtitle_blur": {
        "enabled": True,
        "region_height_pct": 0.18,
        "blur_strength": 18
    },
    "color_grading": {
        "enabled": True,
        "brightness": 0.03,
        "contrast": 1.02,
        "saturation": 1.08
    },
    "bgm": {
        "enabled": False,
        "folder": "assets/bgm",
        "volume": 0.22,
        "style": ""
    },
    "watermark": {
        "enabled": True,
        "text": "Pai Ai Movie Studio",
        "opacity": 0.5,
        "margin": 30,
        "font_size": 40,
        "position": "top_right",
        "style": "text",
        "logo_path": "assets/watermark.png"
    },
    "subtitle_overlay": {
        "enabled": True,
        "style_preset": "box_black",       # "box_black" | "yellow_pop" | "white_stroke" | "cyan_cyber" | "crimson_box"
        "font_name": "Myanmar Text",
        "font_size": 40,
        "bold": True,
        "border_style": 3,
        "outline_width": 3,
        "margin_bottom": 50,
        "max_chars_per_line": 28
    },
    "reels": {
        "enabled": True,
        "width": 1080,
        "height": 1920,
        "blur_background": True,
        "blur_sigma": 25,
        "hook_title_color": "&H00D7FF",
        "font_size": 52,
        "safe_zone_margin": 160
    },
    "action_narration": {
        "enabled": True,
        "min_gap_sec": 18.0,
        "max_bridge_duration_sec": 6.0
    },
    "audio_ducking": {
        "enabled": True,
        "duck_volume": 0.12,
        "ambient_volume": 0.35,
        "normalize_loudness": True,       # bring the finished mix to target_lufs (the narration is quiet and amix halves it)
        "target_lufs": -16.0
    },
    "thumbnail_intro": {
        "enabled": False,                  # If True, prepends 3-second freeze-frame thumbnail intro
        "duration_sec": 3.0
    },
    "outro_protection": {
        "auto_trim": True,                 # Automatically cut trailing dead outro / YouTube subscribe screen after narration ends
        "anti_subscribe_zoom": True,       # Apply subtle 10% center zoom in the final 12s to push corner subscribe cards offscreen
        "outro_card": False,               # Append 3-second Pai AI Movie Studio branded Outro Card
        "trim_end_seconds": 0.0            # Manual fixed seconds to trim from video end (0.0 = use auto_trim)
    },
    "logging": {
        "level": "INFO",                   # DEBUG | INFO | WARNING
        "save_log_file": True
    },
    "qa": {
        "enabled": True,
        "auto_rewrite_threshold": 6,
        "review_output": True,
        "skip_video_qa": False
    }
}

# Subtitle Style Presets for ASS rendering (colors in &HAABBGGRR format)
SUBTITLE_PRESETS = {
    "box_black": {
        "id": "box_black",
        "name": "Cinema Box (Netflix Style)",
        "desc": "အမည်းရောင် Background Box ပါသော ရုပ်ရှင်ရုံသုံး စာတန်း (Contrast အကောင်းဆုံး)",
        "font_size": 40,
        "bold": True,
        "border_style": 3,
        "outline_width": 10,
        "shadow": 2,
        "primary_color": "&H00FFFFFF",   # Pure White
        "outline_color": "&H00000000",   # Black
        "back_color": "&HB0000000",      # 70% Dark Box
        "margin_bottom": 50,
        "badge_color": "#ffffff",
        "badge_bg": "rgba(0,0,0,0.8)",
    },
    "yellow_pop": {
        "id": "yellow_pop",
        "name": "TikTok / Reels Pop (Yellow & Black)",
        "desc": "ရွှေဝါရောင်စာလုံးနှင့် အမည်းရောင်အနားသတ် ထူထူ (Reels / TikTok လူကြိုက်များ)",
        "font_size": 42,
        "bold": True,
        "border_style": 1,
        "outline_width": 4,
        "shadow": 3,
        "primary_color": "&H0000D7FF",   # Vibrant Gold / Yellow
        "outline_color": "&H00000000",   # Black Outline
        "back_color": "&H80000000",
        "margin_bottom": 50,
        "badge_color": "#ffd700",
        "badge_bg": "rgba(255,215,0,0.15)",
    },
    "white_stroke": {
        "id": "white_stroke",
        "name": "Classic Movie White (Drop Shadow)",
        "desc": "အဖြူရောင်သန့်သန့်နှင့် အမည်းရောင် အနားသတ်ရိပ် (Standard Movie Subtitle)",
        "font_size": 40,
        "bold": True,
        "border_style": 1,
        "outline_width": 3.5,
        "shadow": 2.5,
        "primary_color": "&H00FFFFFF",   # Pure White
        "outline_color": "&H00000000",   # Black Outline
        "back_color": "&H90000000",
        "margin_bottom": 50,
        "badge_color": "#ffffff",
        "badge_bg": "rgba(255,255,255,0.15)",
    },
    "cyan_cyber": {
        "id": "cyan_cyber",
        "name": "Cyber Cyan (Sci-Fi & Modern)",
        "desc": "စိမ်းပြာရောင် ခေတ်မီဆန်းသစ်သော စာတန်းပုံစံ (Anime / Sci-Fi Recap)",
        "font_size": 40,
        "bold": True,
        "border_style": 1,
        "outline_width": 3.5,
        "shadow": 2.5,
        "primary_color": "&H00FFFF00",   # Bright Cyan
        "outline_color": "&H00000000",   # Black Outline
        "back_color": "&H80000000",
        "margin_bottom": 50,
        "badge_color": "#00ffff",
        "badge_bg": "rgba(0,255,255,0.15)",
    },
    "crimson_box": {
        "id": "crimson_box",
        "name": "Thriller Crimson (Dark Red Box)",
        "desc": "အနီရင့်ရောင် Background Box ပါသော စိတ်လှုပ်ရှားဖွယ် စာတန်း (Horror / Action Recap)",
        "font_size": 40,
        "bold": True,
        "border_style": 3,
        "outline_width": 10,
        "shadow": 2,
        "primary_color": "&H00FFFFFF",   # Pure White
        "outline_color": "&H00000000",
        "back_color": "&HA0151570",      # Dark Crimson Box
        "margin_bottom": 50,
        "badge_color": "#ff4d4d",
        "badge_bg": "rgba(255,77,77,0.2)",
    },
}

CONFIG_FILE = "config.json"

_config_cache = None
_config_cache_mtime = 0.0


def _parse_env_gemini_keys() -> list:
    """Returns Gemini keys supplied through GEMINI_API_KEYS / GEMINI_API_KEY (12-factor style)."""
    raw = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY")
    if not raw:
        return []
    return [k.strip() for k in raw.replace("\r\n", ",").replace("\n", ",").replace(";", ",").split(",") if k.strip()]

def _deep_merge(base: dict, override: dict) -> dict:
    """
    Merge nested configuration sections without dropping sibling defaults.
    `base` is deep-copied so callers that mutate the result (UI settings, key saves) can never
    corrupt the shared DEFAULT_CONFIG sections.
    """
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged

def load_config() -> dict:
    """Loads config.json if it exists, otherwise creates it with defaults and returns defaults."""
    global _config_cache, _config_cache_mtime
    try:
        mtime = os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else 0.0
    except Exception:
        mtime = 0.0
    if _config_cache is not None and mtime == _config_cache_mtime:
        return _config_cache

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_config = json.load(f)
        except Exception:
            user_config = {}
        merged = _deep_merge(DEFAULT_CONFIG, user_config)
    else:
        # First-time: write the default config file for the user
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
        print(f"[*] Config: Created default config file -> {CONFIG_FILE}")
        merged = copy.deepcopy(DEFAULT_CONFIG)

    # Auto-detect environment GEMINI_API_KEYS or GEMINI_API_KEY (12-Factor App Production Standard)
    parsed_env = _parse_env_gemini_keys()
    if parsed_env:
        if "gemini" not in merged:
            merged["gemini"] = {}
        existing = merged["gemini"].get("api_keys", []) or []
        combined = []
        for k in parsed_env + existing:
            if k and k not in combined:
                combined.append(k)
        merged["gemini"]["api_keys"] = combined

    _config_cache = merged
    _config_cache_mtime = mtime
    return merged

def get(section: str, key: str, fallback=None):
    """Convenience getter: config.get('gemini', 'model')"""
    cfg = load_config()
    return cfg.get(section, {}).get(key, fallback)

def _without_env_only_keys(config_data: dict) -> dict:
    """
    Returns a copy of `config_data` without Gemini keys that exist ONLY in the environment.

    load_config() merges GEMINI_API_KEYS into the in-memory config; saving that merged dict as-is
    would silently copy secrets that the user deliberately kept out of the project folder into
    plaintext config.json. Keys already present in the file stay untouched.

    env var ဖြင့်သာ ထည့်ထားသော key များကို config.json ထဲ မရေးမိစေရန် ဖယ်ထုတ်ပေးသည်။
    """
    env_keys = set(_parse_env_gemini_keys())
    saved = copy.deepcopy(config_data)
    keys = saved.get("gemini", {}).get("api_keys")
    if not env_keys or not isinstance(keys, list):
        return saved

    on_disk = set()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            on_disk = set((json.load(f).get("gemini", {}) or {}).get("api_keys", []) or [])
    except Exception as _exc:
        log_suppressed("brain.config._without_env_only_keys", _exc)
    saved["gemini"]["api_keys"] = [k for k in keys if k not in env_keys or k in on_disk]
    return saved


def save_config(config_data: dict) -> None:
    """Atomically save config (without env-only secrets) and refresh the in-process cache."""
    global _config_cache, _config_cache_mtime
    tmp_file = CONFIG_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(_without_env_only_keys(config_data), f, indent=4, ensure_ascii=False)
    os.replace(tmp_file, CONFIG_FILE)
    _config_cache = config_data
    _config_cache_mtime = os.path.getmtime(CONFIG_FILE)
