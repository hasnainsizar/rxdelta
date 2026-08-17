"""Member cost impact estimation and severity scoring.

Every number here is a range, never a point estimate. See docs/METHODOLOGY.md
for the formula and a worked example, and LIMITATIONS for what the range
deliberately leaves out.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from rxdelta.config import Config
from rxdelta.db import queries
from rxdelta.diff.engine import DiffResult, DrugPlanChange
from rxdelta.types import ChangeType, CostRow, PlanKey

_COST_SOURCE = "beneficiary cost file"


@dataclass(frozen=True)
class TierCost:
    """Monthly member cost at one tier, normalized to one supply length.

    `modal_low` and `modal_high` cover the one combination a member most often
    actually fills, set by [impact.modal]. For a copay tier the two are equal and
    the figure is exact. For a coinsurance tier they are the published dollar
    bounds for that one channel and supply length, which is far tighter than the
    full spread but still a range, so no point estimate is invented. Both are
    None when the plan does not publish that combination.
    """

    low: float | None
    high: float | None
    observations: int
    unpriced_legs: int
    modal_low: float | None = None
    modal_high: float | None = None

    @property
    def known(self) -> bool:
        return self.low is not None and self.high is not None

    @property
    def modal_known(self) -> bool:
        return self.modal_low is not None and self.modal_high is not None


@dataclass(frozen=True)
class ImpactRange:
    """Estimated change in what a member pays, always a range.

    `open_ended` marks a change where one side is not in the CMS data, that is
    a drug joining or leaving the formulary. For those the range describes the
    cost while the drug is covered, and the uncovered side is unknown.
    """

    low: float
    high: float
    direction: int
    open_ended: bool
    priced: bool
    basis: str
    # The modal case: the same change priced only for the pharmacy channel and
    # supply length in [impact.modal]. None when the plan does not publish that
    # combination, which the report marks rather than substituting a neighbour.
    modal_low: float | None = None
    modal_high: float | None = None

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def width(self) -> float:
        return self.high - self.low

    @property
    def modal_known(self) -> bool:
        return self.modal_low is not None and self.modal_high is not None

    @property
    def spans_zero(self) -> bool:
        """The full bound admits both an increase and a decrease."""
        return self.priced and self.low < 0.0 < self.high

    @property
    def modal_direction(self) -> int:
        """+1, -1, or 0 when the modal bound itself straddles zero."""
        if not self.modal_known:
            return 0
        assert self.modal_low is not None and self.modal_high is not None
        if self.modal_low >= 0.0 and self.modal_high > 0.0:
            return 1
        if self.modal_high <= 0.0 and self.modal_low < 0.0:
            return -1
        return 0


@dataclass(frozen=True)
class ChangeGroup:
    """One drug and one change signature, rolled up across the plans it hit."""

    ndc_11: str
    rxcui: str
    drug_name: str | None
    change_types: tuple[ChangeType, ...]
    tier_before: int | None
    tier_after: int | None
    plans: tuple[PlanKey, ...]
    plan_names: tuple[str, ...]
    impact: ImpactRange
    severity: float

    @property
    def plan_count(self) -> int:
        return len(self.plans)


UNPRICED = ImpactRange(
    low=0.0,
    high=0.0,
    direction=0,
    open_ended=False,
    priced=False,
    basis="no matching cost rows in the beneficiary cost file for this plan and tier",
)


def tier_cost(
    config: Config, rows: list[CostRow] | None, *, file_name: str = _COST_SOURCE
) -> TierCost:
    """Spread of monthly member cost across pharmacy channels and supply lengths."""
    if not rows:
        return TierCost(None, None, 0, 0)

    modal_case = config.impact.modal
    lows: list[float] = []
    highs: list[float] = []
    modal_low: float | None = None
    modal_high: float | None = None
    unpriced = 0
    for row in rows:
        days = config.codes.days_for(row.days_supply, file_name)
        if days is None:
            # CMS publishes no length for this code, so the row cannot be put on
            # a common axis. Counted, not guessed at.
            unpriced += len(row.legs)
            continue
        scale = config.impact.normalize_days / days
        for leg in row.legs:
            if not leg.cost_type:
                continue
            kind = config.codes.cost_type_code(leg.cost_type, file_name).kind
            if kind == "not_offered":
                # The channel does not exist for this plan and tier. Its zeros
                # are placeholders, not a free fill.
                continue
            if kind == "copay":
                if leg.cost_amt is None:
                    unpriced += 1
                    continue
                leg_low = leg_high = leg.cost_amt * scale
            else:
                # Coinsurance carries a fraction in COST_AMT, so dollars can only
                # come from the published bounds. In the real files those bounds
                # are almost always 0, which means no bound was published rather
                # than a bound of zero dollars; pricing it as $0 would invent a
                # free drug.
                bounds = [v for v in (leg.cost_min_amt, leg.cost_max_amt) if v]
                if not bounds:
                    unpriced += 1
                    continue
                leg_low, leg_high = min(bounds) * scale, max(bounds) * scale
            lows.append(leg_low)
            highs.append(leg_high)
            if row.days_supply == modal_case.days_supply and leg.channel == modal_case.channel:
                modal_low, modal_high = leg_low, leg_high

    if not lows:
        return TierCost(None, None, 0, unpriced, None, None)
    return TierCost(min(lows), max(highs), len(lows), unpriced, modal_low, modal_high)


def estimate_impact(
    before: TierCost, after: TierCost, direction: int, *, open_ended: bool
) -> ImpactRange:
    """Turn two tier cost ranges into a bounded change in member cost."""
    if open_ended:
        known = after if after.known else before
        if not known.known:
            return UNPRICED
        assert known.low is not None and known.high is not None
        return ImpactRange(
            low=round(min(known.low, known.high), 2),
            high=round(max(known.low, known.high), 2),
            direction=direction,
            open_ended=True,
            priced=True,
            basis=(
                "monthly cost sharing while the drug is on the formulary; the price "
                "without formulary coverage is not published in these files"
            ),
            modal_low=_round(known.modal_low),
            modal_high=_round(known.modal_high),
        )

    if not (before.known and after.known):
        return UNPRICED
    assert before.low is not None and before.high is not None
    assert after.low is not None and after.high is not None

    low = after.low - before.high
    high = after.high - before.low
    modal_low = modal_high = None
    if before.modal_known and after.modal_known:
        assert before.modal_low is not None and before.modal_high is not None
        assert after.modal_low is not None and after.modal_high is not None
        modal_low = after.modal_low - before.modal_high
        modal_high = after.modal_high - before.modal_low
    return ImpactRange(
        low=round(min(low, high), 2),
        high=round(max(low, high), 2),
        direction=direction,
        open_ended=False,
        priced=True,
        basis=(
            "monthly cost sharing before versus after, spread across preferred, "
            "non preferred and mail pharmacies and every published supply length"
        ),
        modal_low=_round(None if modal_low is None else min(modal_low, modal_high or 0.0)),
        modal_high=_round(None if modal_high is None else max(modal_low or 0.0, modal_high)),
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def severity_score(
    config: Config, impact: ImpactRange, plan_count: int, *, adds_restriction: bool
) -> float:
    """Weighted 0 to 100 score used only for sorting. Weights come from config."""
    weights = config.severity
    magnitude = abs(impact.midpoint)
    if impact.open_ended:
        magnitude *= weights.open_ended_factor
    # amount / (amount + reference): 0 at no change, 0.5 at the reference, and
    # asymptotic to 1. A hard clamp flattened every large move onto one value,
    # which cost the ranking exactly where it matters, at the top of the table.
    cost_component = magnitude / (magnitude + weights.cost_reference)

    if impact.direction > 0:
        direction_component = 1.0
    elif impact.direction < 0:
        direction_component = 0.0
    else:
        direction_component = 0.5

    plans_component = min(
        math.log10(1 + max(plan_count, 0)) / math.log10(1 + weights.plan_reference), 1.0
    )
    restriction_component = 1.0 if adds_restriction else 0.0

    weighted = (
        weights.weight_cost * cost_component
        + weights.weight_direction * direction_component
        + weights.weight_plans * plans_component
        + weights.weight_restriction * restriction_component
    )
    return round(100.0 * weighted / weights.weight_total, 2)


def build_groups(conn: sqlite3.Connection, config: Config, result: DiffResult) -> list[ChangeGroup]:
    """Group changes by drug and change signature, price them, and score them."""
    costs_from = queries.cost_rows(conn, result.month_from, config.impact.coverage_level)
    costs_to = queries.cost_rows(conn, result.month_to, config.impact.coverage_level)
    drug_name_map = queries.drug_names(conn)

    # A tier cost depends only on the plan, the tier and the month, but the diff
    # asks for one per change. On the real files that was 2.06 million calls for
    # about 38 thousand distinct answers.
    cost_memo: dict[tuple[int, PlanKey, int | None], TierCost] = {}

    def priced(
        side: int, costs: dict[tuple[PlanKey, int], list[CostRow]], plan: PlanKey, tier: int | None
    ) -> TierCost:
        key = (side, plan, tier)
        hit = cost_memo.get(key)
        if hit is None:
            hit = tier_cost(config, _lookup(costs, plan, tier))
            cost_memo[key] = hit
        return hit

    buckets: dict[
        tuple[str, tuple[ChangeType, ...], int | None, int | None], list[DrugPlanChange]
    ] = defaultdict(list)
    for change in result.changes:
        buckets[(change.ndc_11, change.change_types, change.tier_before, change.tier_after)].append(
            change
        )

    groups: list[ChangeGroup] = []
    for (ndc, change_types, tier_before, tier_after), members in buckets.items():
        open_ended = bool({ChangeType.DRUG_ADDED, ChangeType.DRUG_REMOVED} & set(change_types))
        direction = members[0].net_direction
        per_plan = [
            estimate_impact(
                priced(0, costs_from, c.plan, tier_before),
                priced(1, costs_to, c.plan, tier_after),
                direction,
                open_ended=open_ended,
            )
            for c in members
        ]
        impact = _merge(per_plan, direction=direction, open_ended=open_ended)
        plans = tuple(sorted({c.plan for c in members}))
        names = tuple(dict.fromkeys(c.plan_name for c in members))
        groups.append(
            ChangeGroup(
                ndc_11=ndc,
                rxcui=members[0].rxcui,
                drug_name=drug_name_map.get(members[0].rxcui),
                change_types=change_types,
                tier_before=tier_before,
                tier_after=tier_after,
                plans=plans,
                plan_names=names,
                impact=impact,
                severity=severity_score(
                    config, impact, len(plans), adds_restriction=members[0].adds_restriction
                ),
            )
        )

    return sort_groups(config, groups)


_SORT_VALUES: dict[str, Callable[[ChangeGroup], float | str]] = {
    "severity": lambda g: g.severity,
    "plan_count": lambda g: float(g.plan_count),
    "range_width": lambda g: g.impact.width,
    "abs_midpoint": lambda g: abs(g.impact.midpoint),
    "ndc": lambda g: g.ndc_11,
}


def sort_groups(config: Config, groups: list[ChangeGroup]) -> list[ChangeGroup]:
    """Rank by the key order in [report].sort_order.

    Severity alone leaves large ties on real data, and an unbroken tie falls back
    to whatever order the rows happened to arrive in, which is not a ranking.
    Sorting is applied least significant key first, which is stable, so the
    declared order reads the way it is written.
    """
    ranked = list(groups)
    for key in reversed(config.report.sort_order):
        extract = _SORT_VALUES[key.field]
        if key.field == "ndc":
            ranked.sort(key=lambda g: str(extract(g)), reverse=key.descending)
        else:
            ranked.sort(key=lambda g: float(extract(g)), reverse=key.descending)
    return ranked


def _lookup(
    costs: dict[tuple[PlanKey, int], list[CostRow]], plan: PlanKey, tier: int | None
) -> list[CostRow] | None:
    if tier is None:
        return None
    return costs.get((plan, tier))


def _merge(ranges: list[ImpactRange], *, direction: int, open_ended: bool) -> ImpactRange:
    priced = [r for r in ranges if r.priced]
    if not priced:
        return UNPRICED
    modal_lows = [r.modal_low for r in priced if r.modal_low is not None]
    modal_highs = [r.modal_high for r in priced if r.modal_high is not None]
    return ImpactRange(
        low=round(min(r.low for r in priced), 2),
        high=round(max(r.high for r in priced), 2),
        direction=direction,
        open_ended=open_ended,
        priced=True,
        basis=priced[0].basis,
        modal_low=round(min(modal_lows), 2) if modal_lows else None,
        modal_high=round(max(modal_highs), 2) if modal_highs else None,
    )
