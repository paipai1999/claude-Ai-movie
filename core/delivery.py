"""
Delivery mode - how a finished production is handed over
========================================================
Every studio can finish in one of two ways, and the user chooses per production:

* ``oneclick``  the finished video(s), start to end, nothing else to do (the behaviour so far);
* ``capcut``    the CapCut pipeline: all the heavy work (transcript, translation, voice, clip selection) is done,
                the slow final render is skipped, and ``capcut_package.zip`` is built to finish the edit in CapCut;
* ``both``      the finished video(s) AND the CapCut package.

``wants_video`` / ``wants_package`` answer the two questions every engine asks. ``build_package`` builds the
package when the mode wants one, and ``delivers`` decorates an engine's ``run`` so the three studios (and any
future one) share one implementation instead of three copies.

Pure at import time: the package builder (FFmpeg, project files) is loaded only when a package is built.
"""

import functools
import inspect
import os
from typing import Any, Callable, Dict, Optional

ONE_CLICK = "oneclick"
CAPCUT = "capcut"
BOTH = "both"
MODES = (ONE_CLICK, CAPCUT, BOTH)

_ALIASES = {
    "one_click": ONE_CLICK, "one-click": ONE_CLICK, "finished": ONE_CLICK, "video": ONE_CLICK, "full": ONE_CLICK,
    "capcut_pipeline": CAPCUT, "capcut-pipeline": CAPCUT, "package": CAPCUT, "edit": CAPCUT,
    "all": BOTH,
}


class DeliveryError(RuntimeError):
    """The CapCut package was the deliverable (mode ``capcut``) and could not be built."""


def normalize(mode: Any) -> str:
    """A known mode; anything missing or unknown means the finished video (the behaviour before this option)."""
    text = str(mode or "").strip().lower()
    text = _ALIASES.get(text, text)
    return text if text in MODES else ONE_CLICK


def wants_video(mode: Any) -> bool:
    """False only in the CapCut pipeline, which skips the final render."""
    return normalize(mode) != CAPCUT


def wants_package(mode: Any) -> bool:
    return normalize(mode) in (CAPCUT, BOTH)


def project_dir_of(result: Any) -> Optional[str]:
    """The project folder of an engine result: its ``project_dir`` key, or the folder of any file path it lists."""
    if not isinstance(result, dict):
        return None
    folder = result.get("project_dir")
    if isinstance(folder, str) and os.path.isdir(folder):
        return os.path.abspath(folder)
    for key in ("state_json", "records_data_json"):
        value = result.get(key)
        if isinstance(value, str) and os.path.isabs(value):
            return os.path.dirname(value)
    for value in result.values():
        if isinstance(value, str) and os.path.isabs(value) and os.path.isfile(value):
            return os.path.dirname(value)
    return None


def build_package(project_dir: str, mode: Any, name: str = "") -> Optional[Dict[str, Any]]:
    """
    Builds ``capcut_package.zip`` when ``mode`` wants one and returns its description (None when it does not).
    In ``capcut`` mode a failure raises ``DeliveryError`` (the package IS the result); in ``both`` the finished
    video exists already, so a failure is only reported.
    """
    mode = normalize(mode)
    if not wants_package(mode):
        return None
    from services import capcut_export       # lazy: it pulls in the merger / FFmpeg helpers
    print("\n--- [CapCut package] ---", flush=True)
    try:
        info = capcut_export.export_capcut_package(project_dir, name or os.path.basename(os.path.normpath(project_dir)))
    except Exception as exc:                  # ProjectError, FFmpeg failures, missing files ...
        if mode == CAPCUT:
            raise DeliveryError(f"The CapCut package could not be built: {exc}") from exc
        print(f"[WARN] The finished video is ready, but the CapCut package could not be built: {exc}")
        return None
    print(f"[OK] CapCut package ready -> {info['zip']} ({info['size_mb']} MB, {len(info['files'])} files)")
    return info


def delivers(run: Callable) -> Callable:
    """
    Decorator for an engine's ``run``: adds the keyword ``delivery_mode``. In the CapCut pipeline the final render
    is skipped (``render_video=False``); afterwards the package is built when the mode wants one, and its file
    name is returned as ``result["package"]``.
    """
    params = inspect.signature(run).parameters
    has_render_flag = "render_video" in params

    @functools.wraps(run)
    def wrapper(self, *args, delivery_mode: Any = ONE_CLICK, **kwargs):
        mode = normalize(delivery_mode)
        if has_render_flag and not wants_video(mode):
            kwargs["render_video"] = False
        result = run(self, *args, **kwargs)
        if not wants_package(mode) or (isinstance(result, dict) and result.get("status") == "failed"):
            return result
        folder = project_dir_of(result)
        if not folder:
            if mode == CAPCUT:
                raise DeliveryError("The CapCut package could not be built: the project folder is unknown.")
            return result
        info = build_package(folder, mode)
        if info and isinstance(result, dict):
            result["package"] = info["zip"]
        return result

    signature = inspect.signature(run)
    extra = inspect.Parameter("delivery_mode", inspect.Parameter.KEYWORD_ONLY, default=ONE_CLICK)
    wrapper.__signature__ = signature.replace(parameters=[*signature.parameters.values(), extra])
    return wrapper
