"""How a before or after value is written in a change answer.

The tools, the gold queries and the scorer all compare change values as these
strings, so a tier move is "3" to "4" everywhere and never 3 in one place and
"tier 3" in another.
"""

from __future__ import annotations

from dataclasses import dataclass

from rxdelta.types import ChangeType


@dataclass(frozen=True)
class CoverageState:
    tier: int
    prior_auth: bool
    step_therapy: bool
    quantity_limit: bool
    quantity_limit_amount: float | None
    quantity_limit_days: int | None


def _flag(value: bool) -> str:
    return "yes" if value else "no"


def quantity_limit_text(state: CoverageState) -> str:
    if not state.quantity_limit:
        return "none"
    if state.quantity_limit_amount is None or not state.quantity_limit_days:
        return "yes"
    return f"{state.quantity_limit_amount:g} per {state.quantity_limit_days} days"


def change_values(
    change_type: ChangeType, before: CoverageState | None, after: CoverageState | None
) -> tuple[str | None, str | None]:
    """The (before, after) pair that describes one change type."""
    if change_type is ChangeType.DRUG_ADDED:
        assert after is not None
        return None, f"tier {after.tier}"
    if change_type is ChangeType.DRUG_REMOVED:
        assert before is not None
        return f"tier {before.tier}", None
    assert before is not None and after is not None
    if change_type in (ChangeType.TIER_UP, ChangeType.TIER_DOWN):
        return str(before.tier), str(after.tier)
    if change_type in (ChangeType.PRIOR_AUTH_ADDED, ChangeType.PRIOR_AUTH_REMOVED):
        return _flag(before.prior_auth), _flag(after.prior_auth)
    if change_type in (ChangeType.STEP_THERAPY_ADDED, ChangeType.STEP_THERAPY_REMOVED):
        return _flag(before.step_therapy), _flag(after.step_therapy)
    return quantity_limit_text(before), quantity_limit_text(after)
