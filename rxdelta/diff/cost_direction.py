"""Whether a tier move costs the member more, less, the same, or cannot be told.

Tier numbers are a CMS label, not a price: on the real files a move from tier 3
to tier 6 is usually a move to a $0 tier. This compares what the plan publishes
for the old tier in the earlier month with the new tier in the later month,
like for like: the same coverage phase, supply length and pharmacy channel.
A direction is given only when every channel and supply length published at
both tiers agrees; anything that needs a drug price to decide is unknown.
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from rxdelta.config import Config
from rxdelta.db import queries
from rxdelta.diff.engine import DiffResult, DrugPlanChange
from rxdelta.types import ChangeType, CostLeg, CostRow, PlanKey

_FILE = "beneficiary cost file"
_TIER_MOVES = frozenset({ChangeType.TIER_UP, ChangeType.TIER_DOWN})


class CostDirection(StrEnum):
    HIGHER = "higher"
    LOWER = "lower"
    SAME = "same"
    UNKNOWN = "unknown"

    @property
    def member_impact(self) -> int | None:
        """+1 the member pays more, -1 less, 0 the same, None not known."""
        return {"higher": 1, "lower": -1, "same": 0}.get(self.value)

    @property
    def phrase(self) -> str:
        return {
            "higher": "costs the member more",
            "lower": "costs the member less",
            "same": "same cost to the member",
            "unknown": "cost change not published",
        }[self.value]


def _sign(old: float, new: float) -> CostDirection:
    if new > old:
        return CostDirection.HIGHER
    if new < old:
        return CostDirection.LOWER
    return CostDirection.SAME


def compare_legs(config: Config, old: CostLeg, new: CostLeg) -> CostDirection:
    """One channel at one supply length, old tier against new tier."""
    kinds = []
    for leg in (old, new):
        if not leg.cost_type:
            return CostDirection.UNKNOWN
        kinds.append(config.codes.cost_type_code(leg.cost_type, _FILE).kind)
    if "not_offered" in kinds or old.cost_amt is None or new.cost_amt is None:
        return CostDirection.UNKNOWN
    if kinds[0] == kinds[1]:
        # Two copays compare in dollars, two coinsurances by their rate.
        return _sign(old.cost_amt, new.cost_amt)
    copay, coinsurance = (old, new) if kinds[0] == "copay" else (new, old)
    copay_amt, rate = old.cost_amt, new.cost_amt
    if copay is new:
        copay_amt, rate = rate, copay_amt
    # Settled without a drug price: $0 never costs more than a positive share.
    if copay_amt == 0 and rate == 0:
        return CostDirection.SAME
    if copay_amt == 0 or rate == 0:
        copay_cheaper = copay_amt == 0
    else:
        bounds = [v for v in (coinsurance.cost_min_amt, coinsurance.cost_max_amt) if v]
        if not bounds:
            return CostDirection.UNKNOWN
        if copay_amt < min(bounds):
            copay_cheaper = True
        elif copay_amt > max(bounds):
            copay_cheaper = False
        else:
            return CostDirection.UNKNOWN
    new_is_copay = copay is new
    return CostDirection.LOWER if copay_cheaper == new_is_copay else CostDirection.HIGHER


def tier_direction(
    config: Config, before: list[CostRow] | None, after: list[CostRow] | None
) -> CostDirection:
    """Agreement across every supply length and channel published at both tiers."""
    if not before or not after:
        return CostDirection.UNKNOWN
    after_legs = {(row.days_supply, leg.channel): leg for row in after for leg in row.legs}
    seen: set[CostDirection] = set()
    for row in before:
        for leg in row.legs:
            new = after_legs.get((row.days_supply, leg.channel))
            if new is not None:
                seen.add(compare_legs(config, leg, new))
    seen.discard(CostDirection.UNKNOWN)
    moving = seen - {CostDirection.SAME}
    if len(moving) == 1:
        return moving.pop()
    if not moving and seen:
        return CostDirection.SAME
    return CostDirection.UNKNOWN


_Costs = dict[tuple[PlanKey, int], list[CostRow]]


def directions(
    conn: sqlite3.Connection,
    config: Config,
    result: DiffResult,
    costs: tuple[_Costs, _Costs] | None = None,
) -> list[CostDirection | None]:
    """The cost direction of each change in the diff, None when it moved no tier.

    `costs` takes the two months' cost rows when the caller has already read them.
    """
    level = config.impact.coverage_level
    before, after = costs or (
        queries.cost_rows(conn, result.month_from, level),
        queries.cost_rows(conn, result.month_to, level),
    )
    memo: dict[tuple[PlanKey, int, int], CostDirection] = {}

    def one(change: DrugPlanChange) -> CostDirection | None:
        if not _TIER_MOVES & set(change.change_types):
            return None
        assert change.tier_before is not None and change.tier_after is not None
        key = (change.plan, change.tier_before, change.tier_after)
        if key not in memo:
            memo[key] = tier_direction(
                config,
                before.get((change.plan, change.tier_before)),
                after.get((change.plan, change.tier_after)),
            )
        return memo[key]

    return [one(c) for c in result.changes]


def net_direction(change_types: tuple[ChangeType, ...], cost: CostDirection | None) -> int:
    """+1 toward the member paying or being restricted more, -1 less, 0 mixed.

    A tier move counts by its cost direction, not by its tier number; when that
    is the same or unknown it counts for nothing either way.
    """
    impacts = {t.member_impact for t in change_types if t not in _TIER_MOVES}
    if cost is not None and cost.member_impact:
        impacts.add(cost.member_impact)
    elif cost is None:
        impacts |= {t.member_impact for t in change_types if t in _TIER_MOVES}
    if impacts == {1}:
        return 1
    if impacts == {-1}:
        return -1
    return 0
