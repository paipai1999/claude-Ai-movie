from brain.diagnostics import log_suppressed
from datetime import datetime, timezone, timedelta

# DST-aware Pacific Time via zoneinfo (works on Windows too).
_PT_ZONE = None
try:
    from zoneinfo import ZoneInfo
    _PT_ZONE = ZoneInfo("America/Los_Angeles")
except Exception as _exc:
    log_suppressed("brain.tracker.<module>", _exc)  # Falls back to UTC-8 fixed offset in get_google_utc_date()

def get_google_utc_date() -> str:
    """Returns current date in Pacific Time (PT) — DST-aware where possible.
    Google Gemini API resets daily limits at midnight Pacific Time.
    """
    if _PT_ZONE is not None:
        try:
            pt_time = datetime.now(_PT_ZONE)
            return pt_time.strftime("%Y-%m-%d")
        except Exception as _exc:
            log_suppressed("brain.tracker.get_google_utc_date", _exc)
    pt_time = datetime.now(timezone.utc) - timedelta(hours=8)
    return pt_time.strftime("%Y-%m-%d")

def _mask_key(key: str) -> str:
    return key[:6] + "..." + key[-4:] if len(key) > 10 else key

def load_usage_db() -> dict:
    """Loads usage database from unified SQLite store (movie_metadata.db)."""
    from brain.sqlite_store import load_sqlite_usage_db
    current_utc = get_google_utc_date()
    return load_sqlite_usage_db(current_utc)

def reserve_model_usage(key: str, model: str, limit: int) -> bool:
    """Atomically checks limit and reserves 1 usage in unified SQLite store."""
    from brain.sqlite_store import reserve_sqlite_model_usage
    current_utc = get_google_utc_date()
    masked = _mask_key(key)
    return reserve_sqlite_model_usage(masked, model, limit, current_utc)

def record_key_usage(key: str, model: str, status: str = "success"):
    """Records completion status in unified SQLite store."""
    from brain.sqlite_store import record_sqlite_model_usage
    current_utc = get_google_utc_date()
    masked = _mask_key(key)
    record_sqlite_model_usage(masked, model, status, current_utc)

def get_key_usage(key: str) -> dict:
    """Returns usage dictionary for a specific key from unified SQLite store."""
    db = load_usage_db()
    masked = _mask_key(key)
    return db.get("keys", {}).get(masked, {})
