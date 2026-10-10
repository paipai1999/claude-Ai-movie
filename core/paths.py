"""
Path Safety Utilities (ဖိုင်လမ်းကြောင်း လုံခြုံရေး helper များ)
=============================================================
Single place for turning user-supplied names (project names, folder items) into
filesystem paths that are guaranteed to stay inside a trusted base directory.

ဤ module သည် အသုံးပြုသူ ပေးပို့လာသော နာမည်များ (`project_name`, ဖိုင်အမည်) ကို
`outputs/` ကဲ့သို့ ယုံကြည်ရသော ဖိုဒါအပြင်သို့ ထွက်မသွားစေရန် စစ်ဆေး/သန့်စင်ပေးသည်။
"""

import os
import re
import time
from typing import Optional

# Characters that are illegal in Windows file names or act as path separators / drive markers.
_UNSAFE_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Names Windows refuses to create as files/folders.
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_project_name(name: Optional[str], fallback: str = "", ascii_only: bool = False) -> str:
    """
    Converts an arbitrary project name into a single, safe folder name.

    - Path separators, drive colons, wildcard/control characters and '..' sequences become '_'
      (so '../../x' or 'C:\\Windows' can never leave the output folder).
    - Unicode letters INCLUDING combining marks (Burmese vowel signs, asat, medials) are kept
      unless ascii_only=True, in which case only [A-Za-z0-9_-] survive.
    - Falls back to `fallback`, then to a timestamped name, when nothing usable remains.

    နာမည်ထဲမှ `/`, `\\`, `..`, `:` စသည်တို့ကို `_` ဖြင့် အစားထိုးပြီး မြန်မာစာလုံးများကို မပျက်စေဘဲ ထိန်းသိမ်းသည်။
    """
    def _clean(raw: Optional[str]) -> str:
        text = str(raw or "").strip()
        if ascii_only:
            text = re.sub(r"[^a-zA-Z0-9_\-]", "_", text)
        else:
            text = _UNSAFE_NAME_CHARS.sub("_", text)
            text = re.sub(r"\.{2,}", "_", text)   # no '..' anywhere inside the name
        text = text.strip("._ ")
        if text.upper() in _WINDOWS_RESERVED:
            text = f"_{text}"
        return text[:120]

    return _clean(name) or _clean(fallback) or f"project_{int(time.time())}"


def resolve_inside(base_dir: str, *parts: str, allow_base: bool = False) -> Optional[str]:
    """
    Joins `parts` onto `base_dir` and returns the absolute path ONLY if the result is
    inside `base_dir`. Returns None for traversal attempts, absolute-path overrides,
    or (unless allow_base=True) the base directory itself.

    `base_dir` ထဲမှာသာ ရှိသော path ဖြစ်မှ ပြန်ပေးပြီး မဟုတ်ပါက None ပြန်ပေးသည်။
    """
    base = os.path.abspath(base_dir)
    if not parts or not all(isinstance(p, str) and p for p in parts):
        return None
    candidate = os.path.abspath(os.path.join(base, *parts))
    try:
        if os.path.commonpath([base, candidate]) != base:
            return None
    except ValueError:  # different drives on Windows
        return None
    if candidate == base and not allow_base:
        return None
    return candidate
