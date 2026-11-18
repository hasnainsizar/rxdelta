"""Name normalization shared by every name source and by the matcher."""

from __future__ import annotations

import re

# Keep a decimal point only between two digits, so "0.25 MG" stays one token.
_DECIMAL = re.compile(r"(?<=\d)\.(?=\d)")
_NON_WORD = re.compile(r"[^a-z0-9\x00]+")
_TRAILING_BRAND = re.compile(r"\[([^\[\]]+)\]\s*$")
_POINT = "\x00"


def normalize_name(text: str) -> str:
    """Lowercase, drop punctuation and collapse whitespace."""
    lowered = _DECIMAL.sub(_POINT, text.lower())
    spaced = _NON_WORD.sub(" ", lowered).replace(_POINT, ".")
    return " ".join(spaced.split())


def tokens(text: str) -> tuple[str, ...]:
    return tuple(normalize_name(text).split())


def bracket_brand(full_name: str) -> str | None:
    """The brand RxNorm puts in brackets at the end of a branded concept name."""
    found = _TRAILING_BRAND.search(full_name.strip())
    return found.group(1).strip() if found else None
