"""
Channel profiles (Brand Kit)
============================
A profile is a named bundle of wizard answers - narrator voice, subtitle look, watermark, outro,
protection and audio settings - so a channel's "house style" is one click instead of fifteen.

Profiles hold *settings only*: per-video values (the input, project name, thumbnail title, trim,
story hint, resume flag) are deliberately not allowed, and nothing here can carry an API key.
Option names and value types are checked against the single option model (``core.options``), so a
profile can never smuggle in something the wizard itself does not offer.

Stored in ``profiles.json`` (git-ignored) with an atomic write.
"""

import datetime
import json
import os
import re
import threading
import uuid
from typing import Any, Dict, List, Optional

from brain.diagnostics import log_suppressed
from core import user_files
from core.options import BY_NAME, DOWNLOAD, ENGINES
from core.user_files import settings_path

PROFILES_FILE = "profiles.json"
# On Google Colab the profiles live on Google Drive (see core.user_files); tests patch this name.
DRIVE_DIR = user_files.DRIVE_DIR
MAX_PROFILES = 50
MAX_NAME_CHARS = 60
MAX_PROFILE_BYTES = 8_000

# Options that belong to one specific video / run, not to a channel's style.
PER_VIDEO_OPTIONS = frozenset({
    "engine_mode", "input", "project_name", "custom_thumb_title", "trim_end", "no_smart_trim",
    "context_hint", "resume", "force_whisper",
})

_lock = threading.Lock()


class ProfileError(ValueError):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _path() -> str:
    return settings_path("PAI_PROFILES_FILE", PROFILES_FILE, DRIVE_DIR)


def _load_all() -> List[Dict[str, Any]]:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log_suppressed("services.profiles._load_all", exc)
        return []
    profiles = data.get("profiles") if isinstance(data, dict) else None
    return [p for p in profiles if isinstance(p, dict) and p.get("id")] if isinstance(profiles, list) else []


def _save_all(profiles: List[Dict[str, Any]]) -> None:
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"profiles": profiles}, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _path())


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ── validation ─────────────────────────────────────────────────────────────────
def _type_ok(value: Any, expected: type) -> bool:
    if value is None:
        return True                      # null = "let the default decide"
    if expected is bool:
        return isinstance(value, bool)
    if expected in (int, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def validate_options(engine: str, options: Any) -> Dict[str, Any]:
    """Returns the clean options dict, or raises ``ProfileError`` naming the first problem."""
    if not isinstance(options, dict):
        raise ProfileError("options must be an object.")
    clean: Dict[str, Any] = {}
    for name, value in options.items():
        opt = BY_NAME.get(name)
        if opt is None:
            raise ProfileError(f"Unknown option: {name!r}.")
        if name in PER_VIDEO_OPTIONS:
            raise ProfileError(f"{name!r} belongs to a single video and cannot be saved in a profile.")
        if not opt.applies_to(engine):
            raise ProfileError(f"{name!r} does not apply to the {engine} engine.")
        if not _type_ok(value, opt.type):
            raise ProfileError(f"{name!r} must be {opt.type.__name__}.")
        if opt.type is str and isinstance(value, str) and len(value) > 500:
            raise ProfileError(f"{name!r} is too long.")
        clean[name] = value
    if len(json.dumps(clean, ensure_ascii=False).encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ProfileError("The profile is too large.")
    return clean


def _clean_name(name: Any) -> str:
    text = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", "", str(name or ""))).strip()
    if not text:
        raise ProfileError("The profile needs a name.")
    if len(text) > MAX_NAME_CHARS:
        raise ProfileError(f"The profile name is too long (max {MAX_NAME_CHARS}).")
    return text


# ── CRUD ───────────────────────────────────────────────────────────────────────
def list_profiles() -> List[Dict[str, Any]]:
    with _lock:
        return _load_all()


def get_profile(profile_id: str) -> Dict[str, Any]:
    for p in list_profiles():
        if p["id"] == profile_id:
            return p
    raise ProfileError("Profile not found.", code="not_found")


def save_profile(name: Any, engine_mode: Any, options: Any, profile_id: Optional[str] = None) -> Dict[str, Any]:
    """Creates a profile, or replaces the one with ``profile_id``. Names are unique (case-insensitive)."""
    engine = str(engine_mode or "")
    if engine == DOWNLOAD or engine not in ENGINES:
        raise ProfileError("Choose a studio engine for the profile (recap, subtitle or hardsub).")
    clean_name = _clean_name(name)
    clean_options = validate_options(engine, options)

    with _lock:
        profiles = _load_all()
        clash = next((p for p in profiles if p["name"].casefold() == clean_name.casefold() and p["id"] != profile_id), None)
        if clash:
            raise ProfileError(f"A profile named {clean_name!r} already exists.", code="duplicate")
        existing = next((p for p in profiles if p["id"] == profile_id), None) if profile_id else None
        if profile_id and not existing:
            raise ProfileError("Profile not found.", code="not_found")
        if not existing and len(profiles) >= MAX_PROFILES:
            raise ProfileError(f"At most {MAX_PROFILES} profiles are kept - delete one first.", code="limit")

        record = {
            "id": existing["id"] if existing else uuid.uuid4().hex[:12],
            "name": clean_name,
            "engine_mode": engine,
            "options": clean_options,
            "created_at": existing["created_at"] if existing else _now(),
            "updated_at": _now(),
        }
        profiles = [record if p["id"] == record["id"] else p for p in profiles] if existing else profiles + [record]
        _save_all(profiles)
        return record


def delete_profile(profile_id: str) -> None:
    with _lock:
        profiles = _load_all()
        kept = [p for p in profiles if p["id"] != profile_id]
        if len(kept) == len(profiles):
            raise ProfileError("Profile not found.", code="not_found")
        _save_all(kept)
