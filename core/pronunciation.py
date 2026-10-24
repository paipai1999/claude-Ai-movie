"""
Pronunciation dictionary
========================
Edge TTS often mis-reads names and loanwords in Burmese. The dictionary lets the user fix that once:
``ဂျွန်  =>  ဂျွန်း`` (or ``Spielberg => စပီးလ်ဘတ်``) and every voice-over line is spoken with the replacement.

It changes only the text sent to the voice - the script, the subtitles and the SRT keep the original spelling.

* matching is single-pass (a replacement is never replaced again) and longest-entry-first;
* Latin entries match case-insensitively and only as whole words; other scripts match as plain substrings
  (Burmese has no word spaces);
* ``signature_for`` lets the voice cache treat only the lines a dictionary change actually alters as stale.

The entries live in ``dictionary.json`` (git-ignored; on Google Drive when running on Colab).
"""

import json
import os
import re
import threading
from typing import Any, Dict, Iterable, List, Optional

from brain.diagnostics import log_suppressed
from core.user_files import settings_path

DICTIONARY_FILE = "dictionary.json"
MAX_ENTRIES = 500
MAX_TERM_CHARS = 80

_LATIN_TERM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '\-.]*$")
_SEPARATORS = ("=>", "->", "→", "=", "\t")   # accepted between "from" and "to" when parsing text lines
_lock = threading.Lock()


class DictionaryError(ValueError):
    pass


def _path() -> str:
    return settings_path("PAI_DICTIONARY_FILE", DICTIONARY_FILE)


# ── entries: validation and (de)serialisation ───────────────────────────────────
def clean_entries(raw: Any) -> List[Dict[str, str]]:
    """Validated, de-duplicated entries (last one wins per ``from``, compared case-insensitively)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DictionaryError("entries must be a list of {from, to} objects.")
    by_key: Dict[str, Dict[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise DictionaryError("Every entry must be an object with 'from' and 'to'.")
        source = re.sub(r"\s+", " ", str(item.get("from", ""))).strip()
        target = re.sub(r"\s+", " ", str(item.get("to", ""))).strip()
        if not source:
            continue                       # a blank row in the editor is not an error
        if len(source) > MAX_TERM_CHARS or len(target) > MAX_TERM_CHARS:
            raise DictionaryError(f"Entries are limited to {MAX_TERM_CHARS} characters: {source[:20]!r}...")
        if source == target:
            continue
        by_key[source.casefold()] = {"from": source, "to": target}
    entries = list(by_key.values())
    if len(entries) > MAX_ENTRIES:
        raise DictionaryError(f"At most {MAX_ENTRIES} entries are kept.")
    return entries


def parse_lines(text: str) -> List[Dict[str, str]]:
    """Entries from an editor text, one ``from => to`` per line (also ``->``, ``=``, a tab). ``#`` starts a comment."""
    entries: List[Dict[str, str]] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in _SEPARATORS:
            if sep in line:
                left, right = line.split(sep, 1)
                entries.append({"from": left, "to": right})
                break
        else:
            raise DictionaryError(f"Line without a separator (use 'from => to'): {line[:40]!r}")
    return clean_entries(entries)


def format_lines(entries: Iterable[Dict[str, str]]) -> str:
    return "\n".join(f"{e['from']} => {e['to']}" for e in entries)


# ── applying ────────────────────────────────────────────────────────────────────
def _piece(entry: Dict[str, str]) -> str:
    term = re.escape(entry["from"])
    if _LATIN_TERM.match(entry["from"]):
        return rf"(?<![A-Za-z0-9]){term}(?![A-Za-z0-9])"
    return term


def compile_entries(entries: Iterable[Dict[str, str]]) -> Optional["re.Pattern"]:
    ordered = sorted(entries, key=lambda e: len(e["from"]), reverse=True)
    if not ordered:
        return None
    return re.compile("|".join(_piece(e) for e in ordered), re.IGNORECASE)


def apply_entries(text: str, entries: List[Dict[str, str]], _compiled: Optional["re.Pattern"] = None) -> str:
    """``text`` with every dictionary term replaced (one pass, longest entry first)."""
    if not text or not entries:
        return text
    pattern = _compiled or compile_entries(entries)
    lookup = {e["from"].casefold(): e["to"] for e in entries}
    return pattern.sub(lambda m: lookup.get(m.group(0).casefold(), m.group(0)), text)


class Pronouncer:
    """A loaded dictionary ready to apply many lines (the pattern is compiled once)."""

    def __init__(self, entries: Optional[List[Dict[str, str]]] = None):
        self.entries = clean_entries(entries or [])
        self._pattern = compile_entries(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def apply(self, text: str) -> str:
        return apply_entries(text, self.entries, self._pattern)


# ── storage ─────────────────────────────────────────────────────────────────────
def load_entries() -> List[Dict[str, str]]:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log_suppressed("core.pronunciation.load_entries", exc)
        return []
    try:
        return clean_entries(data.get("entries") if isinstance(data, dict) else None)
    except DictionaryError as exc:
        log_suppressed("core.pronunciation.load_entries", exc)
        return []


def save_entries(raw: Any) -> List[Dict[str, str]]:
    """Validates and stores the dictionary (atomic write); returns the clean entries."""
    entries = clean_entries(raw)
    with _lock:
        path = _path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"entries": entries}, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    return entries
