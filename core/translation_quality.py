"""
Translation quality checks
==========================
Pure functions that look at what the model returned for a batch of lines and say what is wrong with
it, so the translator can ask again with *specific* corrections instead of silently shipping:

* a line that is missing / a placeholder / chatter ("Here is the translation: ...")
* a line that was not translated (still English/Chinese/...) or still has loose English words
* a line that just copies the source text
* two neighbouring lines that say the same thing, or a line that re-states the previous one
* a line far too long for the time slot (the voice would have to race)

Everything here is deterministic and offline: no Gemini call, no I/O.
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from brain.burmese_utils import has_untranslated_foreign_script
from core.text_compare import normalize, squash, trigram_similarity

# Myanmar block, Myanmar Extended-A / -B.
_MYANMAR = re.compile(r"[က-႟ꩠ-ꩿꧠ-꧿]")
# Scripts that must never appear in a Burmese line: kana, CJK, hangul, Cyrillic, Georgian, Thai,
# Devanagari, Arabic. (Latin is judged separately because a brand name may legitimately survive.)
_FOREIGN_SCRIPT = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿가-힣Ѐ-ӿႠ-ჿ฀-๿ऀ-ॿ؀-ۿ]"
)
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_PLACEHOLDER = re.compile(
    r"^[\s.\-–—_*#…·?!]*$|^(?:n/?a|none|null|todo|tbd|translation|narration|untranslated|\[[^\]]*\]|<[^>]*>)$",
    re.IGNORECASE,
)
_META = re.compile(
    r"```|^\s*[\(\[]?\s*(?:note|translation|translated|narration)\s*[:\)\]]|"
    r"\b(?:here(?:'s| is| are)|as an ai|i (?:cannot|can't|am unable))\b",
    re.IGNORECASE,
)

# Tunables - generous on purpose: a false alarm costs an extra API call, a missed one ships a bad line.
MAX_ACRONYM_LETTERS = 5      # an all-caps token this short is an acronym that the sanitiser transliterates
MAX_LATIN_WORDS = 1          # more loose English words than this in a Burmese line is a problem
MAX_LATIN_SHARE = 0.50       # ... or Latin letters making up more than this share of all letters (mostly English)
DUPLICATE_SIMILARITY = 0.88  # char-trigram Jaccard of neighbouring lines
DUPLICATE_MIN_CHARS = 14     # (letters + marks) shorter lines repeat legitimately ("အင်း၊ ဟုတ်ပါတယ်")
BOUNDARY_ECHO_CHARS = 14     # tail of the previous line that must not reappear at the head of this one
OVER_BUDGET_RATIO = 1.45     # narration longer than this x max_chars cannot be spoken in its slot

# Problems that make a line unusable and worth another model call. The others (duplicate neighbour,
# restated context, over-long) are reported but never block a line.
RETRY_CODES = frozenset({
    "missing", "placeholder", "meta_text", "untranslated", "foreign_script", "latin_words", "copied_source",
})

# After every retry a line with one of these problems has no usable narration in the target language: it is empty,
# a placeholder, a copy of the source, or not in Burmese script at all. Voicing it would read foreign text aloud
# in the Burmese voice, so such a line is left silent (and not shown as a subtitle) until a human fixes it.
UNSPEAKABLE_CODES = frozenset({"missing", "placeholder", "untranslated", "copied_source"})


def is_unspeakable(issues) -> bool:
    """True when the problems still present on a line mean it must not be voiced."""
    return any(code in UNSPEAKABLE_CODES for code in (issues or ()))


def block_is_silent(block) -> bool:
    """
    True for a script block that must be neither voiced nor subtitled: the writer marked it ``untranslated``, or it is
    still flagged for review with a problem that leaves it without usable narration (this also covers projects made
    before the ``untranslated`` mark existed). A human edit clears the flag and the review state together.
    """
    if not isinstance(block, dict):
        return False
    return bool(block.get("untranslated")) or (bool(block.get("needs_review")) and is_unspeakable(block.get("issues")))


# Human-readable correction sentences, one per problem code (sent back to the model).
_ADVICE = {
    "missing": "no translation was returned for this line - translate it",
    "placeholder": "the narration is empty or placeholder text - write the real narration",
    "meta_text": "the narration contains commentary or notes - return only the narration itself",
    "untranslated": "the narration is not in Burmese script - translate it into Burmese",
    "foreign_script": "the narration contains Chinese/Japanese/Korean/Cyrillic/Thai characters - transliterate them into Burmese script",
    "latin_words": "the narration still contains English words{detail} - transliterate names and loanwords phonetically into Burmese script",
    "copied_source": "the narration just repeats the source text - translate it",
    "duplicate": "the narration is almost identical to the previous line - give each line its own content",
    "repeat_boundary": "the narration starts by repeating the end of the previous line - do not repeat the context lines",
    "over_budget": "the narration is{detail} - shorten it so it can be spoken in the time slot",
}


@dataclass(frozen=True)
class Problem:
    seg_id: int
    code: str
    detail: str = ""

    def advice(self) -> str:
        template = _ADVICE.get(self.code, self.code)
        return template.format(detail=f" ({self.detail})" if self.detail else "")


def has_myanmar(text: str) -> bool:
    return bool(_MYANMAR.search(text or ""))


def latin_words(text: str) -> List[str]:
    """Loose English words in a line, ignoring short all-caps acronyms (those get transliterated)."""
    return [
        w for w in _LATIN_WORD.findall(text or "")
        if not (w.isupper() and len(w) <= MAX_ACRONYM_LETTERS)
    ]


def _script_problems(seg_id: int, text: str) -> List[Problem]:
    """Problems specific to a Burmese target: wrong script, loose English."""
    out: List[Problem] = []
    if not _MYANMAR.search(text):
        out.append(Problem(seg_id, "untranslated"))
        return out   # nothing more to say about a line that is not Burmese at all
    if _FOREIGN_SCRIPT.search(text):
        out.append(Problem(seg_id, "foreign_script"))
    loose = latin_words(text)
    # Count Myanmar code points, not str.isalpha(): vowel signs and medials are combining marks.
    latin_letters = sum(len(w) for w in loose)
    share = latin_letters / max(1, len(_MYANMAR.findall(text)) + latin_letters)
    if len(loose) > MAX_LATIN_WORDS or share > MAX_LATIN_SHARE:
        out.append(Problem(seg_id, "latin_words", ", ".join(loose[:4])))
    return out


def find_problems(
    narrations: Mapping[int, Optional[str]],
    *,
    burmese_target: bool = True,
    source: Optional[Mapping[int, str]] = None,
    max_chars: Optional[Mapping[int, int]] = None,
    previous_tail: str = "",
) -> List[Problem]:
    """
    Problems in ``{segment id: narration}``, in id order.

    ``source``: original text per id (detects copied text and "same narration for different lines").
    ``max_chars``: per-id character budget (detects over-long lines).
    ``previous_tail``: the end of the narration just before the first id (catches a model that
    re-states its context). A missing id should be passed as ``None``.
    """
    problems: List[Problem] = []
    prev_text = previous_tail or ""
    prev_id: Optional[int] = None

    for seg_id in sorted(narrations):
        text = narrations[seg_id]
        text = "" if text is None else str(text).strip()

        if not text:
            problems.append(Problem(seg_id, "missing"))
            continue
        if _PLACEHOLDER.match(text):
            problems.append(Problem(seg_id, "placeholder"))
            continue
        if _META.search(text):
            problems.append(Problem(seg_id, "meta_text"))

        src = (source or {}).get(seg_id, "")
        if burmese_target:
            problems.extend(_script_problems(seg_id, text))
        if src and len(normalize(src)) > 3 and normalize(text) == normalize(src):
            problems.append(Problem(seg_id, "copied_source"))

        # Two lines legitimately repeat when the speaker repeats ("Come back!" twice).
        same_source = bool(source) and prev_id is not None and normalize(source.get(prev_id, "")) == normalize(src)
        if prev_text and not same_source:
            if (
                min(len(squash(prev_text)), len(squash(text))) >= DUPLICATE_MIN_CHARS
                and trigram_similarity(prev_text, text) >= DUPLICATE_SIMILARITY
            ):
                problems.append(Problem(seg_id, "duplicate"))
            else:
                tail = squash(prev_text)[-BOUNDARY_ECHO_CHARS:]
                head = squash(text)[: BOUNDARY_ECHO_CHARS * 3]
                if len(tail) >= BOUNDARY_ECHO_CHARS and tail in head:
                    problems.append(Problem(seg_id, "repeat_boundary"))

        budget = (max_chars or {}).get(seg_id)
        if budget and len(text) > budget * OVER_BUDGET_RATIO:
            problems.append(Problem(seg_id, "over_budget", f"{len(text)} characters, limit {budget}"))

        prev_text, prev_id = text, seg_id
    return problems


def ids_with_problems(problems: Iterable[Problem]) -> Set[int]:
    return {p.seg_id for p in problems}


def group_by_id(problems: Iterable[Problem]) -> Dict[int, List[Problem]]:
    grouped: Dict[int, List[Problem]] = {}
    for p in problems:
        grouped.setdefault(p.seg_id, []).append(p)
    return grouped


def feedback_text(problems: Iterable[Problem]) -> str:
    """The correction list appended to a retry prompt (one bullet per affected line)."""
    lines = []
    for seg_id, items in sorted(group_by_id(problems).items()):
        lines.append(f"- id {seg_id}: " + "; ".join(p.advice() for p in items))
    return "\n".join(lines)


def find_damaged_lines(segments: Sequence[Mapping[str, Any]]) -> List[int]:
    """
    Indices of subtitle/dialogue records (``{"original", "burmese"}``) that need a repair call: empty,
    still in a foreign script, or - the common case when a batch failed and the source text was copied in -
    never translated (no Burmese, a copy of the source, loose English words, model chatter).
    Sound-only lines ("♪", "...") and lines whose source is already Burmese are skipped.
    """
    damaged = set()
    texts: Dict[int, str] = {}
    sources: Dict[int, str] = {}
    for idx, seg in enumerate(segments):
        burmese = str(seg.get("burmese", "")).strip()
        original = str(seg.get("original", ""))
        if not burmese or has_untranslated_foreign_script(burmese):
            damaged.add(idx)
        elif any(ch.isalpha() for ch in original) and not has_myanmar(original):
            texts[idx], sources[idx] = burmese, original
    damaged.update(p.seg_id for p in find_problems(texts, burmese_target=True, source=sources) if p.code in RETRY_CODES)
    return sorted(damaged)
