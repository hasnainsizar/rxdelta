"""Compare two snapshots and classify every change per drug and plan."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from rxdelta.db import queries
from rxdelta.types import ChangeType, DiffError, DrugCoverage, PlanKey

_PairChange = tuple[str, str, tuple[ChangeType, ...], "DrugCoverage | None", "DrugCoverage | None"]


@dataclass(frozen=True)
class DrugPlanChange:
    """Everything that changed for one drug in one plan between two snapshots."""

    plan: PlanKey
    plan_name: str
    ndc_11: str
    rxcui: str
    change_types: tuple[ChangeType, ...]
    tier_before: int | None
    tier_after: int | None
    before: DrugCoverage | None
    after: DrugCoverage | None

    @property
    def net_direction(self) -> int:
        """+1 if the change points toward the member paying more, -1 less, 0 mixed."""
        impacts = {c.member_impact for c in self.change_types}
        if impacts == {1}:
            return 1
        if impacts == {-1}:
            return -1
        return 0

    @property
    def adds_restriction(self) -> bool:
        return any(c.is_restriction_added for c in self.change_types)


@dataclass
class DiffResult:
    month_from: str
    month_to: str
    plan_filter: str | None
    changes: list[DrugPlanChange] = field(default_factory=list)
    plans_compared: int = 0
    plans_added: list[PlanKey] = field(default_factory=list)
    plans_removed: list[PlanKey] = field(default_factory=list)
    drugs_from: int = 0
    drugs_to: int = 0

    @property
    def affected_plans(self) -> int:
        return len({c.plan for c in self.changes})

    @property
    def affected_drugs(self) -> int:
        return len({c.ndc_11 for c in self.changes})

    def counts_by_type(self) -> dict[ChangeType, int]:
        counter: Counter[ChangeType] = Counter()
        for change in self.changes:
            counter.update(change.change_types)
        return {t: counter[t] for t in ChangeType if counter[t]}

    def counts_by_plan(self) -> dict[PlanKey, int]:
        counter: Counter[PlanKey] = Counter()
        for change in self.changes:
            counter[change.plan] += 1
        return dict(counter.most_common())


def classify(before: DrugCoverage | None, after: DrugCoverage | None) -> tuple[ChangeType, ...]:
    """Classify one drug in one formulary. A pair can carry several changes."""
    if before is None and after is None:
        return ()
    if before is None:
        return (ChangeType.DRUG_ADDED,)
    if after is None:
        return (ChangeType.DRUG_REMOVED,)

    changes: list[ChangeType] = []
    if after.tier_level > before.tier_level:
        changes.append(ChangeType.TIER_UP)
    elif after.tier_level < before.tier_level:
        changes.append(ChangeType.TIER_DOWN)

    if after.prior_auth and not before.prior_auth:
        changes.append(ChangeType.PRIOR_AUTH_ADDED)
    elif before.prior_auth and not after.prior_auth:
        changes.append(ChangeType.PRIOR_AUTH_REMOVED)

    if after.step_therapy and not before.step_therapy:
        changes.append(ChangeType.STEP_THERAPY_ADDED)
    elif before.step_therapy and not after.step_therapy:
        changes.append(ChangeType.STEP_THERAPY_REMOVED)

    changes.extend(_classify_quantity_limit(before, after))
    return tuple(changes)


def _classify_quantity_limit(before: DrugCoverage, after: DrugCoverage) -> list[ChangeType]:
    if after.quantity_limit and not before.quantity_limit:
        return [ChangeType.QUANTITY_LIMIT_ADDED]
    if before.quantity_limit and not after.quantity_limit:
        return [ChangeType.QUANTITY_LIMIT_REMOVED]
    if not (before.quantity_limit and after.quantity_limit):
        return []

    before_daily = before.daily_quantity()
    after_daily = after.daily_quantity()
    if before_daily is None or after_daily is None:
        return []
    if after_daily < before_daily:
        return [ChangeType.QUANTITY_LIMIT_TIGHTENED]
    if after_daily > before_daily:
        return [ChangeType.QUANTITY_LIMIT_LOOSENED]
    return []


def diff_snapshots(
    conn: sqlite3.Connection,
    month_from: str,
    month_to: str,
    *,
    plan_filter: str | None = None,
) -> DiffResult:
    """Compare two loaded months, optionally narrowed to one contract."""
    if month_from == month_to:
        raise DiffError(f"--from and --to are both {month_from}; nothing to compare")
    for month in (month_from, month_to):
        if not queries.month_is_loaded(conn, month):
            raise DiffError(f"Month {month} is not loaded. Run: rxdelta load --month {month}")

    plans_from = {p.key: p for p in queries.plans(conn, month_from, plan_filter)}
    plans_to = {p.key: p for p in queries.plans(conn, month_to, plan_filter)}
    if plan_filter and not (plans_from or plans_to):
        raise DiffError(f"No plans matched contract {plan_filter!r} in either month")

    shared = sorted(set(plans_from) & set(plans_to))
    result = DiffResult(
        month_from=month_from,
        month_to=month_to,
        plan_filter=plan_filter,
        plans_compared=len(shared),
        plans_added=sorted(set(plans_to) - set(plans_from)),
        plans_removed=sorted(set(plans_from) - set(plans_to)),
        drugs_from=queries.distinct_ndc_count(conn, month_from),
        drugs_to=queries.distinct_ndc_count(conn, month_to),
    )

    # Formularies are shared across plans, so classify each formulary pair once
    # and fan the result out. A plan that moves to a different formulary between
    # months gets its own pair, which is the point of keying on the pair.
    pair_cache: dict[tuple[str, str], list[_PairChange]] = {}

    for key in shared:
        pair = (plans_from[key].formulary_id, plans_to[key].formulary_id)
        if pair not in pair_cache:
            pair_cache[pair] = _diff_formulary_pair(conn, month_from, month_to, pair)
        plan_name = plans_to[key].plan_name or plans_from[key].plan_name
        for ndc, rxcui, change_types, before, after in pair_cache[pair]:
            result.changes.append(
                DrugPlanChange(
                    plan=key,
                    plan_name=plan_name,
                    ndc_11=ndc,
                    rxcui=rxcui,
                    change_types=change_types,
                    tier_before=before.tier_level if before else None,
                    tier_after=after.tier_level if after else None,
                    before=before,
                    after=after,
                )
            )
    return result


def _diff_formulary_pair(
    conn: sqlite3.Connection, month_from: str, month_to: str, pair: tuple[str, str]
) -> list[_PairChange]:
    """Classify one formulary pair.

    The candidate query has already discarded rows that are byte for byte the
    same in both months. Deciding what a surviving difference means is still
    done here, by classify.
    """
    formulary_from, formulary_to = pair
    out: list[_PairChange] = []
    for before, after in queries.candidate_changes(
        conn, month_from, month_to, formulary_from, formulary_to
    ):
        change_types = classify(before, after)
        if not change_types:
            continue
        source = after or before
        assert source is not None
        out.append((source.ndc_11, source.rxcui, change_types, before, after))
    out.sort(key=lambda item: item[0])
    return out
