"""Value normalization: NDC codes, plan keys, flags and numbers.

Every function that can fail returns a result object carrying a reason rather
than raising, so the loader can route the row to rejected_rows and keep going.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rxdelta.types import PlanKey

_NON_DIGIT = re.compile(r"[^0-9]")
_TRUE_FLAGS = frozenset({"Y", "YES", "1", "TRUE", "T"})
_FALSE_FLAGS = frozenset({"N", "NO", "0", "FALSE", "F", ""})

_PAD_BY_POLICY = {
    "assume_4_4_2": (4, 4, 2),
    "assume_5_3_2": (5, 3, 2),
    "assume_5_4_1": (5, 4, 1),
}


@dataclass(frozen=True)
class NdcResult:
    """Either an 11 digit NDC or the reason the input could not become one."""

    ndc_11: str | None
    raw: str
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.ndc_11 is not None


def normalize_ndc(raw: str, *, unhyphenated_10_policy: str = "reject") -> NdcResult:
    """Normalize an NDC to 11 digits in 5-4-2 form.

    Hyphenated 10 digit codes carry their segment lengths, so the zero goes in
    the segment that is short. An unhyphenated 10 digit code does not, and no
    amount of inspection recovers it, so the policy decides.
    """
    original = raw
    text = (raw or "").strip()
    if not text:
        return NdcResult(None, original, "empty NDC")

    if "-" in text:
        segments = text.split("-")
        if len(segments) != 3:
            return NdcResult(
                None, original, f"expected 3 hyphenated NDC segments, got {len(segments)}"
            )
        if any(not s or _NON_DIGIT.search(s) for s in segments):
            return NdcResult(None, original, "empty or non-numeric segment in hyphenated NDC")
        lengths = tuple(len(s) for s in segments)
        if lengths == (5, 4, 2):
            return NdcResult("".join(segments), original)
        if lengths == (4, 4, 2):
            return NdcResult("0" + segments[0] + segments[1] + segments[2], original)
        if lengths == (5, 3, 2):
            return NdcResult(segments[0] + "0" + segments[1] + segments[2], original)
        if lengths == (5, 4, 1):
            return NdcResult(segments[0] + segments[1] + "0" + segments[2], original)
        return NdcResult(
            None, original, f"unrecognized NDC segment lengths {'-'.join(map(str, lengths))}"
        )

    if _NON_DIGIT.search(text):
        return NdcResult(None, original, "non-numeric characters in NDC")

    if len(text) == 11:
        return NdcResult(text, original)

    if len(text) == 10:
        pad = _PAD_BY_POLICY.get(unhyphenated_10_policy)
        if pad is None:
            return NdcResult(
                None,
                original,
                "ambiguous unhyphenated 10 digit NDC, segment lengths are not recoverable",
            )
        first, second, _third = pad
        if pad == (4, 4, 2):
            return NdcResult("0" + text, original)
        if pad == (5, 3, 2):
            return NdcResult(text[:first] + "0" + text[first:], original)
        cut = first + second
        return NdcResult(text[:cut] + "0" + text[cut:], original)

    return NdcResult(None, original, f"NDC has {len(text)} digits, expected 10 or 11")


def normalize_plan_key(contract_id: str, plan_id: str, segment_id: str) -> PlanKey:
    """Plan ids and segment ids are zero padded fixed width in CMS files, and
    some exports drop the leading zeros. Pad them back so the composite key
    joins across files."""
    return PlanKey(
        contract_id=contract_id.strip().upper(),
        plan_id=plan_id.strip().zfill(3),
        segment_id=segment_id.strip().zfill(3),
    )


def parse_flag(value: str, *, field: str) -> bool:
    text = (value or "").strip().upper()
    if text in _TRUE_FLAGS:
        return True
    if text in _FALSE_FLAGS:
        return False
    raise ValueError(f"{field} must be a yes/no flag, got {value!r}")


def parse_int(value: str, *, field: str) -> int:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} is required and was empty")
    try:
        return int(float(text))
    except ValueError as exc:
        raise ValueError(f"{field} must be a whole number, got {value!r}") from exc


def parse_optional_int(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def parse_optional_float(value: str) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_code(value: str, *, field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} is required and was empty")
    return text
