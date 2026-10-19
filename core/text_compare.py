"""
Script-safe text comparison
===========================
Helpers the quality checks use to decide "are these two lines the same?".

Python's ``\\w`` does not match combining marks, so a regex such as ``[\\W_]+`` silently deletes
Myanmar vowel signs and medials (and Thai/Devanagari ones) - ``ကျေး`` would compare as ``က``. These
helpers keep letters, marks and digits and only drop punctuation, symbols and whitespace.
"""

import unicodedata


def normalize(text: str) -> str:
    """Case-folded text with punctuation/symbols/whitespace collapsed to single spaces."""
    folded = unicodedata.normalize("NFC", str(text or "")).casefold()
    kept = "".join(ch if unicodedata.category(ch)[0] in "LMN" else " " for ch in folded)
    return " ".join(kept.split())


def squash(text: str) -> str:
    """``normalize`` with the spaces removed (Burmese has no word spaces, so spacing is noise)."""
    return normalize(text).replace(" ", "")


def trigram_similarity(a: str, b: str) -> float:
    """Jaccard similarity of character trigrams (0..1); works for any script."""
    a, b = squash(a), squash(b)
    if len(a) < 3 or len(b) < 3:
        return 1.0 if a and a == b else 0.0
    grams_a = {a[i:i + 3] for i in range(len(a) - 2)}
    grams_b = {b[i:i + 3] for i in range(len(b) - 2)}
    return len(grams_a & grams_b) / len(grams_a | grams_b)
