"""
Diagnostics (Debug ခြေရာခံ helper)
=================================
Many best-effort code paths (temp-file cleanup, optional features, DB bookkeeping) deliberately
ignore errors. Silently ignoring them makes real problems invisible, so those handlers call
`log_suppressed()` instead of a bare `pass`:

  - normal runs      : nothing is printed (no log noise)
  - PAI_DEBUG=1      : every ignored exception is printed with its location

ရည်ရွယ်ချက်ရှိရှိ လျစ်လျူရှုထားသော error များကို `PAI_DEBUG=1` ဖြင့် run လျှင် မြင်ရစေရန်။
"""

import os


def debug_enabled() -> bool:
    return os.environ.get("PAI_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def log_suppressed(where: str, exc: BaseException) -> None:
    """Reports an intentionally ignored exception when PAI_DEBUG is on. Never raises."""
    try:
        if debug_enabled():
            print(f"[DEBUG] ignored in {where}: {type(exc).__name__}: {exc}")
    except Exception:
        pass
