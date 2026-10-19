"""
Story memory (translation bible)
================================
Translating 20 lines at a time, the model forgets who "he" is and spells the same name three
different ways. Before the first batch we make ONE extra call over the whole (sampled) transcript
and ask for a compact "bible": characters with gender and Burmese spelling, places, recurring
terms, relationships and tone. It is rendered as a short text block that every batch prompt carries,
so names, genders and honorifics stay consistent from the first line to the last.

The call is optional and best-effort: no key, a short clip, or any failure simply returns ``""``.
"""

import hashlib
import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from brain.diagnostics import log_suppressed

# Short clips do not need a bible (the batch itself already holds the whole story).
MIN_LINES_FOR_MEMORY = 15
# Transcript characters sent to the model; longer films are sampled evenly across their length.
MAX_INPUT_CHARS = 26000
# Caps on the rendered block so it never crowds out the actual batch.
_MAX_ITEMS = {"characters": 14, "places": 8, "terms": 12, "relationships": 8}
_MAX_FIELD_CHARS = 90
_MAX_BLOCK_CHARS = 2600

_SYSTEM_PROMPT = (
    "You are a script supervisor preparing a translation bible for a film/series so that several "
    "translators working on different parts of it stay perfectly consistent. You only read the "
    "transcript and report facts; you never translate the dialogue itself."
)


def context_key(lines: Sequence[str], target_language: str, movie_name: str = "") -> str:
    """Fingerprint of what the bible was built from, so a cached bible is reused only when valid."""
    h = hashlib.sha1()
    h.update(f"{target_language}|{movie_name}|{len(lines)}".encode("utf-8"))
    for line in list(lines[:6]) + list(lines[-6:]):
        h.update(str(line).encode("utf-8"))
    return h.hexdigest()[:16]


def sample_transcript(lines: Sequence[str], max_chars: int = MAX_INPUT_CHARS) -> str:
    """Numbered transcript text within ``max_chars``; if it does not fit, every k-th line is taken."""
    cleaned = [(i + 1, str(t).strip()) for i, t in enumerate(lines) if str(t).strip()]
    total = sum(len(t) + 8 for _, t in cleaned)
    if total > max_chars and cleaned:
        step = total / max_chars
        picked, acc = [], 0.0
        for item in cleaned:
            acc += 1.0
            if acc >= step:
                picked.append(item)
                acc -= step
        cleaned = picked or cleaned[:1]
    out, used = [], 0
    for idx, text in cleaned:
        row = f"{idx}. {text}"
        if used + len(row) + 1 > max_chars:
            break
        out.append(row)
        used += len(row) + 1
    return "\n".join(out)


def build_user_prompt(transcript_text: str, target_language: str, movie_name: str, hint: str = "") -> str:
    lang = target_language.title()
    hint_block = f"USER GUIDANCE (trust this over your own guess):\n{hint.strip()}\n\n" if hint and hint.strip() else ""
    return (
        f"Movie title: {movie_name or 'Unknown'}\n"
        f"Target language of the translation: {lang}\n\n"
        f"{hint_block}"
        "Read this transcript (numbered lines, sampled across the whole film) and produce the bible as JSON:\n"
        "{\n"
        '  "summary": "<=80 words: premise, setting, main conflict",\n'
        '  "tone": "e.g. gritty crime thriller / light family comedy / wuxia epic",\n'
        '  "characters": [{"name": "name as heard", "target_name": "its spelling in the target language\'s '
        'script", "gender": "male|female|unknown", "role": "short role", "address": "how others address them (titles/kinship)"}],\n'
        '  "places": [{"name": "...", "target_name": "..."}],\n'
        '  "terms": [{"term": "recurring term/organisation/title", "target": "fixed rendering"}],\n'
        '  "relationships": ["A is B\'s father", "..."]\n'
        "}\n"
        f"Rules: give every name ONE fixed spelling in {lang} script (phonetic transliteration, never Latin letters). "
        "Only include people/places that actually recur or matter. Use \"unknown\" instead of guessing a gender.\n\n"
        f"TRANSCRIPT:\n{transcript_text}"
    )


def _clip(value: Any, limit: int = _MAX_FIELD_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _list_of_dicts(value: Any) -> List[Dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def render_bible(data: Any) -> str:
    """Compact prompt block from the model's JSON (tolerates missing/odd fields); "" if nothing usable."""
    if not isinstance(data, dict):
        return ""
    lines: List[str] = []

    summary = _clip(data.get("summary"), 420)
    if summary:
        lines.append(f"Summary: {summary}")
    tone = _clip(data.get("tone"))
    if tone:
        lines.append(f"Tone: {tone}")

    chars = []
    for c in _list_of_dicts(data.get("characters"))[: _MAX_ITEMS["characters"]]:
        name, target = _clip(c.get("name"), 40), _clip(c.get("target_name"), 40)
        if not (name or target):
            continue
        gender = str(c.get("gender") or "").strip().lower()
        details = ", ".join(x for x in (
            gender if gender in ("male", "female") else "",
            _clip(c.get("role"), 40),
            f'addressed as "{_clip(c.get("address"), 40)}"' if _clip(c.get("address"), 40) else "",
        ) if x)
        chars.append(f"- {name} => {target}" + (f" ({details})" if details else ""))
    if chars:
        lines.append("Characters (use exactly these spellings and genders):")
        lines.extend(chars)

    for key, label, a, b in (("places", "Places", "name", "target_name"), ("terms", "Terms", "term", "target")):
        pairs = []
        for item in _list_of_dicts(data.get(key))[: _MAX_ITEMS[key]]:
            left, right = _clip(item.get(a), 40), _clip(item.get(b), 40)
            if left and right:
                pairs.append(f"{left} => {right}")
        if pairs:
            lines.append(f"{label}: " + "; ".join(pairs))

    rels = [_clip(r, 80) for r in (data.get("relationships") or [])[: _MAX_ITEMS["relationships"]] if isinstance(r, str) and r.strip()]
    if rels:
        lines.append("Relationships: " + "; ".join(rels))

    if not lines:
        return ""
    block = "STORY BIBLE (reference for consistency only - never output it):\n" + "\n".join(lines)
    return block if len(block) <= _MAX_BLOCK_CHARS else block[: _MAX_BLOCK_CHARS - 1].rstrip() + "…"


def parse_bible(block: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    The structured form of a rendered bible (the inverse of ``render_bible``), for display in the Script Editor.
    Returns None when ``block`` is empty or not a bible.
    """
    if not block or not str(block).startswith("STORY BIBLE"):
        return None
    out: Dict[str, Any] = {"summary": "", "tone": "", "characters": [], "places": [], "terms": [], "relationships": []}
    section = None
    for line in str(block).splitlines()[1:]:
        line = line.rstrip()
        if line.startswith("Summary: "):
            out["summary"], section = line[len("Summary: "):], None
        elif line.startswith("Tone: "):
            out["tone"], section = line[len("Tone: "):], None
        elif line.startswith("Characters"):
            section = "characters"
        elif line.startswith("Places: ") or line.startswith("Terms: "):
            key = "places" if line.startswith("Places") else "terms"
            for pair in line.split(": ", 1)[1].split("; "):
                left, sep, right = pair.partition(" => ")
                if sep:
                    out[key].append({"name": left.strip(), "target": right.strip()})
            section = None
        elif line.startswith("Relationships: "):
            out["relationships"] = [r for r in line[len("Relationships: "):].split("; ") if r]
            section = None
        elif section == "characters" and line.startswith("- "):
            body = line[2:]
            details = ""
            if body.endswith(")") and " (" in body:
                body, _, details = body[:-1].rpartition(" (")
            name, sep, target = body.partition(" => ")
            if sep:
                out["characters"].append({"name": name.strip(), "target": target.strip(), "details": details.strip()})
    return out


def _parse_json(raw: str) -> Optional[Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError as exc:
                log_suppressed("agents.story_memory._parse_json", exc)
    return None


def build_story_context(
    lines: Sequence[str],
    *,
    target_language: str,
    movie_name: str,
    api_key: Any,
    model: str,
    call: Callable[..., Any],
    hint: str = "",
    log: Callable[[str], None] = print,
) -> str:
    """
    The rendered bible for ``lines`` (source-language dialogue in order), or ``""``.
    ``call`` is ``brain.gemini_client.call_gemini`` (injected so tests need no network).
    """
    if not api_key or len([t for t in lines if str(t).strip()]) < MIN_LINES_FOR_MEMORY:
        return ""
    transcript = sample_transcript(lines)
    if not transcript:
        return ""
    try:
        raw, _model = call(
            _SYSTEM_PROMPT,
            build_user_prompt(transcript, target_language, movie_name, hint),
            api_key,
            model,
            temperature=0.1,
            max_tokens=4096,
            response_mime_type="application/json",
        )
    except InterruptedError:
        raise   # the user pressed Stop - never swallow that
    except Exception as exc:
        log(f"[WARN] Story memory skipped ({type(exc).__name__}: {str(exc)[:120]}); translating without it.")
        return ""
    block = render_bible(_parse_json(raw))
    if block:
        log(f"[*] Story memory ready ({block.count(chr(10)) + 1} lines): names, genders and terms stay consistent.")
    return block
