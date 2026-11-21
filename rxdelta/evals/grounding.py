"""Number grounding: every number a runner states must come from a tool result.

Numbers are pulled out of the final text (integers, decimals, dollar amounts,
percentages, tier numbers, both ends of a range). Numbers that are not claims
are dropped: numbered list markers at the start of a line, ones the question
itself contains, years and YYYY-MM months,
identifiers like NDCs, drug strengths ("1 mg") and mix ratios ("75/25"), and
anything inside a drug or plan name that a tool returned. Every number left
must equal, after normalizing format and sign, a number somewhere in the run's
tool results.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.])(?<![A-Za-z0-9]-)"
    r"(?P<tok>[-−]?\$\s?\d[\d,]*(?:\.\d+)?"  # money
    r"|\d+(?:-\d+)+"  # hyphenated: months, NDCs, ranges like 10-20
    r"|[-−]?\d[\d,]*(?:\.\d+)?%?)"  # plain numbers and percentages
    r"(?![A-Za-z0-9_])"
)
_UNITS = re.compile(r"^\s*(mg|mcg|ml|g|iu|unt|units?|mg/ml)\b", re.IGNORECASE)
_MONTH = re.compile(r"^\d{4}-\d{2}$")
# "1." or "2)" opening a line numbers a list item; it is not a claim.
_LIST_MARKER = re.compile(r"^(\s*)\d{1,3}[.)](?=\s)", re.MULTILINE)
_DASH_RANGE = re.compile(r"(\d)\s*[-–—]\s*(?=[-−]?\$)")
_NAME_KEYS = frozenset(
    {"display_name", "name", "matched_on", "product", "plan_name", "contract_name", "label"}
)


@dataclass(frozen=True)
class Grounding:
    claims: tuple[str, ...]
    ungrounded: tuple[str, ...]

    @property
    def grounded(self) -> bool:
        return not self.ungrounded


def _key(value: float) -> str:
    return f"{round(abs(value), 2):.2f}"


def _candidates(token: str) -> list[float]:
    """Values one number token may stand for: a percentage may be a fraction."""
    text = token.replace("\u2212", "-").replace("$", "").replace(",", "").strip()
    percent = text.endswith("%")
    number = float(text.rstrip("%"))
    return [number, number / 100.0] if percent else [number]


def _split(token: str) -> list[str]:
    """A short hyphenated pair such as 10-20 is a range: two separate numbers."""
    if re.fullmatch(r"\d+(?:-\d+)+", token):
        return token.split("-")
    return [token]


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group("tok"), m.start("tok"), m.end("tok")) for m in _TOKEN.finditer(text)]


def _is_claim(token: str, start: int, end: int, text: str) -> bool:
    if _MONTH.match(token):
        return False
    if "-" in token.strip("-−") and not token.startswith(("$", "-$")):
        parts = token.split("-")
        if len(parts) > 2 or any(len(p) > 3 for p in parts):
            return False
    bare = token.lstrip("-−$ ").rstrip("%")
    if re.fullmatch(r"(19|20)\d\d", bare):
        return False
    if end < len(text) and text[end] == "/" and text[end + 1 : end + 2].isdigit():
        return False
    if start > 0 and text[start - 1] == "/":
        return False
    return not _UNITS.match(text[end:])


def _strip_names(text: str, names: Iterable[str]) -> str:
    for name in sorted({n for n in names if len(n) >= 3}, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    return text


def claimed_numbers(text: str, *, question: str = "", names: Iterable[str] = ()) -> list[str]:
    """Normalized numbers the text claims, excluding anything that is not a claim."""
    unlisted = _LIST_MARKER.sub(r"\1", text)
    cleaned = _DASH_RANGE.sub(r"\1 to ", _strip_names(unlisted, names))
    asked = {
        _key(v)
        for tok, _, _ in _tokens(question)
        for part in _split(tok)
        for v in _candidates(part)
    }
    claims: list[str] = []
    for token, start, end in _tokens(cleaned):
        if not _is_claim(token, start, end, cleaned):
            continue
        for part in _split(token.strip()):
            if not any(_key(v) in asked for v in _candidates(part)):
                claims.append(part)
    return claims


def _walk(node: Any, numbers: set[str], names: set[str], key: str = "") -> None:
    if isinstance(node, bool) or node is None:
        return
    if isinstance(node, int | float):
        numbers.add(_key(float(node)))
        return
    if isinstance(node, str):
        if key in _NAME_KEYS:
            names.add(node)
        for token, _, _ in _tokens(node):
            for part in _split(token):
                numbers.update(_key(v) for v in _candidates(part))
        return
    if isinstance(node, dict):
        for k, v in node.items():
            _walk(v, numbers, names, str(k))
    elif isinstance(node, list | tuple):
        for item in node:
            _walk(item, numbers, names, key)


def evidence(tool_results: Iterable[Any]) -> tuple[set[str], set[str]]:
    """Every normalized number and every name string in the tool results."""
    numbers: set[str] = set()
    names: set[str] = set()
    for result in tool_results:
        _walk(result, numbers, names)
    return numbers, names


def check(final_text: str, question: str, tool_results: Iterable[Any]) -> Grounding:
    numbers, names = evidence(tool_results)
    claims = claimed_numbers(final_text, question=question, names=names)
    ungrounded = tuple(
        claim for claim in claims if not any(_key(v) in numbers for v in _candidates(claim))
    )
    return Grounding(claims=tuple(claims), ungrounded=ungrounded)
