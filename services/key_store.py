"""
API key line parsing
====================
Shared by ``POST /api/keys/save`` and ``POST /api/keys/check``: the dashboard shows keys masked
(``AIzaSy...abcd``), so a pasted line may be a full key, a mask of a stored key, or several keys
separated by commas / new lines. Nothing here logs or returns a raw key.
"""

from typing import Iterable, List, Tuple

# A mask carries 6 leading and 4 trailing characters (see ``mask_key``); each end must be at least this long.
_MIN_MASK_END = 4


def mask_key(key: str) -> str:
    """The only form in which a key ever leaves the server."""
    key = str(key)
    return key[:6] + "..." + key[-4:] if len(key) > 10 else "***"


def split_key_tokens(lines: Iterable[str]) -> List[str]:
    """Every non-empty token of the pasted lines (commas and new lines both separate keys)."""
    tokens: List[str] = []
    for line in lines:
        tokens.extend(t.strip() for t in str(line).replace(",", "\n").split("\n") if t.strip())
    return tokens


def resolve_key_lines(lines: Iterable[str], existing_keys: Iterable[str]) -> Tuple[List[str], List[str]]:
    """
    Returns ``(keys, unresolved_masks)``: de-duplicated full keys in input order, and the masked
    tokens that do not match exactly one stored key. A mask is only honoured when BOTH ends are given
    and exactly one stored key matches, so a bare "AIza..." can never act as a wildcard that reveals
    or silently deletes a stored key.
    """
    existing = [str(k) for k in existing_keys]
    keys: List[str] = []
    unresolved: List[str] = []
    for token in split_key_tokens(lines):
        if "..." in token:
            parts = token.split("...")
            prefix, suffix = parts[0].strip(), parts[-1].strip()
            matches = [
                k for k in existing
                if len(prefix) >= _MIN_MASK_END and len(suffix) >= _MIN_MASK_END and k.startswith(prefix) and k.endswith(suffix)
            ]
            if len(matches) != 1:
                unresolved.append(token)
                continue
            token = matches[0]
        if token and token not in keys:
            keys.append(token)
    return keys, unresolved
