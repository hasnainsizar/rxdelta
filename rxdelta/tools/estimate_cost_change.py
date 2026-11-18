"""estimate_cost_change: the existing cost range logic for one drug and one scope."""

from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import BaseModel, Field, model_validator

from rxdelta.db import queries
from rxdelta.diff.impact import TierCost, estimate_impact, tier_cost
from rxdelta.limitations import ESTIMATE_NOTE, LIMITATIONS, OPEN_ENDED_NOTE
from rxdelta.tools.common import (
    FormularyId,
    Month,
    PlanId,
    Rxcui,
    ToolInput,
    display_names,
    formulary_key,
    month_problem,
    parse_plan_id,
    plan_text,
    rxcui_known,
)
from rxdelta.tools.context import ToolContext
from rxdelta.tools.envelope import ErrorCode, Provenance, ToolResult, failure, success
from rxdelta.types import PlanKey

NAME = "estimate_cost_change"
DESCRIPTION = (
    "Estimate how much a member's monthly cost sharing for one drug changes between two "
    "months on one plan (plan_id) or across the plans using one formulary (formulary_id). "
    "Exactly one of the two is required. Returns the before and after monthly ranges and the "
    "change range (low, high) in US dollars, scaled to a 30 day supply, using the published "
    "cost sharing for the drug's tier. Only tier moves change the figure; if the tier did not "
    "change the change range is zero. It is a range, not a prediction, and it does not cover "
    "the deductible, catastrophic phase, low income subsidy or rebates; the result lists "
    "everything it leaves out. "
    'Example input: {"rxcui": "8800151", "from_month": "2026-02", '
    '"to_month": "2026-03", "plan_id": "H1234-001"}'
)
EXAMPLE: dict[str, Any] = {
    "rxcui": "8800151",
    "from_month": "2026-02",
    "to_month": "2026-03",
    "plan_id": "H1234-001",
}


class EstimateCostInput(ToolInput):
    rxcui: Rxcui = Field(description="RXCUI from find_drug.")
    from_month: Month = Field(description="Baseline month, YYYY-MM.")
    to_month: Month = Field(description="Comparison month, YYYY-MM.")
    formulary_id: FormularyId | None = Field(default=None, description="One formulary.")
    plan_id: PlanId | None = Field(default=None, description="One plan, H1234-001.")

    @model_validator(mode="after")
    def _scope(self) -> EstimateCostInput:
        if bool(self.formulary_id) == bool(self.plan_id):
            raise ValueError("pass exactly one of formulary_id or plan_id")
        if self.from_month == self.to_month:
            raise ValueError("from_month and to_month must be different months")
        return self


class DollarRange(BaseModel):
    low: float
    high: float


class PlanCost(BaseModel):
    """`priced` means the change range is known; `before` and `after` stay empty
    when the plan publishes no dollar amount for the tier."""

    plan_id: str
    tier_before: int | None
    tier_after: int | None
    before: DollarRange | None
    after: DollarRange | None
    change: DollarRange | None
    priced: bool


class EstimateCostOutput(BaseModel):
    rxcui: str
    display_name: str
    from_month: str
    to_month: str
    formulary_id: str | None
    plan_id: str | None
    tier_before: int | None
    tier_after: int | None
    tier_changed: bool
    before: DollarRange | None
    after: DollarRange | None
    change: DollarRange | None
    open_ended: bool
    priced: bool
    currency: str = "USD"
    basis: str
    plans: list[PlanCost]
    leaves_out: list[str]
    estimate_note: str


def _tier(conn: sqlite3.Connection, month: str, formulary: str, rxcui: str) -> int | None:
    row = conn.execute(
        "SELECT tier_level FROM formulary WHERE snapshot_month = ? AND formulary_id = ? "
        "AND rxcui = ? ORDER BY ndc_11 LIMIT 1",
        (month, formulary, rxcui),
    ).fetchone()
    return None if row is None else int(row[0])


def _ndcs(conn: sqlite3.Connection, month: str, formulary: str, rxcui: str) -> list[str]:
    rows = conn.execute(
        "SELECT ndc_11 FROM formulary WHERE snapshot_month = ? AND formulary_id = ? AND rxcui = ? "
        "ORDER BY ndc_11",
        (month, formulary, rxcui),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _range(cost: TierCost) -> DollarRange | None:
    if cost.low is None or cost.high is None:
        return None
    return DollarRange(low=round(cost.low, 2), high=round(cost.high, 2))


def _span(ranges: list[DollarRange | None]) -> DollarRange | None:
    known = [r for r in ranges if r is not None]
    if not known:
        return None
    return DollarRange(low=min(r.low for r in known), high=max(r.high for r in known))


def _scope_plans(
    conn: sqlite3.Connection, args: EstimateCostInput
) -> tuple[list[tuple[PlanKey, str, str]], str | None]:
    """(plan, formulary before, formulary after) in scope, or an error message."""
    before = {p.key: p.formulary_id for p in queries.plans(conn, args.from_month)}
    after = {p.key: p.formulary_id for p in queries.plans(conn, args.to_month)}
    if args.plan_id:
        key = parse_plan_id(args.plan_id)
        assert key is not None
        if key not in before or key not in after:
            return [], (
                f"Plan {plan_text(key)} is not in both the {args.from_month} and "
                f"{args.to_month} plan files. Use find_plan."
            )
        return [(key, before[key], after[key])], None
    keys = sorted(k for k in set(before) & set(after) if args.formulary_id in (before[k], after[k]))
    if not keys:
        return [], (
            f"No plan uses formulary {args.formulary_id} in both {args.from_month} and "
            f"{args.to_month}. Use find_plan to look formulary IDs up."
        )
    return [(k, before[k], after[k]) for k in keys], None


def estimate_cost_change(
    args: EstimateCostInput, ctx: ToolContext
) -> ToolResult[EstimateCostOutput]:
    months = [args.from_month, args.to_month]
    provenance = Provenance(tool=NAME, args=args.model_dump(mode="json"), months=months)
    config = ctx.config
    level = config.impact.coverage_level
    conn = ctx.connect()
    try:
        problem = month_problem(conn, *months)
        if problem:
            return failure(ErrorCode.MONTH_NOT_LOADED, problem, provenance)
        if not rxcui_known(conn, args.rxcui):
            return failure(
                ErrorCode.UNKNOWN_DRUG,
                f"RXCUI {args.rxcui} is not in any loaded formulary or name source. "
                "Use find_drug to look a drug up by name.",
                provenance,
            )
        scope, scope_error = _scope_plans(conn, args)
        if scope_error:
            return failure(ErrorCode.UNKNOWN_PLAN, scope_error, provenance)
        name = display_names(conn, [args.rxcui])[args.rxcui]
        costs_before = queries.cost_rows(conn, args.from_month, level)
        costs_after = queries.cost_rows(conn, args.to_month, level)

        plans: list[PlanCost] = []
        keys: list[str] = []
        bases: list[str] = []
        open_ended = False
        for key, f_before, f_after in scope:
            tier_before = _tier(conn, args.from_month, f_before, args.rxcui)
            tier_after = _tier(conn, args.to_month, f_after, args.rxcui)
            if tier_before is None and tier_after is None:
                continue
            keys += [
                formulary_key(args.from_month, f_before, n)
                for n in _ndcs(conn, args.from_month, f_before, args.rxcui)
            ]
            keys += [
                formulary_key(args.to_month, f_after, n)
                for n in _ndcs(conn, args.to_month, f_after, args.rxcui)
            ]
            cost_before = tier_cost(
                config, costs_before.get((key, tier_before)) if tier_before else None
            )
            cost_after = tier_cost(
                config, costs_after.get((key, tier_after)) if tier_after else None
            )
            if tier_before:
                keys.append(f"beneficiary_cost:{args.from_month}:{key}:{level}:{tier_before}")
            if tier_after:
                keys.append(f"beneficiary_cost:{args.to_month}:{key}:{level}:{tier_after}")
            if tier_before == tier_after:
                # Same tier, same published cost sharing: the change is known to
                # be zero even when the plan publishes no dollar amount for it.
                change: DollarRange | None = DollarRange(low=0.0, high=0.0)
                priced = True
            else:
                plan_open = tier_before is None or tier_after is None
                open_ended = open_ended or plan_open
                rising = tier_after is None or (
                    tier_before is not None and tier_after > tier_before
                )
                impact = estimate_impact(
                    cost_before, cost_after, 1 if rising else -1, open_ended=plan_open
                )
                priced = impact.priced
                change = DollarRange(low=impact.low, high=impact.high) if priced else None
                bases.append(impact.basis)
            plans.append(
                PlanCost(
                    plan_id=plan_text(key),
                    tier_before=tier_before,
                    tier_after=tier_after,
                    before=_range(cost_before),
                    after=_range(cost_after),
                    change=change,
                    priced=priced,
                )
            )
    finally:
        conn.close()

    provenance.row_keys = keys
    where = f"plan {args.plan_id}" if args.plan_id else f"formulary {args.formulary_id}"
    if not plans:
        empty = EstimateCostOutput(
            rxcui=args.rxcui,
            display_name=name,
            from_month=args.from_month,
            to_month=args.to_month,
            formulary_id=args.formulary_id,
            plan_id=args.plan_id,
            tier_before=None,
            tier_after=None,
            tier_changed=False,
            before=None,
            after=None,
            change=None,
            open_ended=False,
            priced=False,
            basis="the drug is not on this formulary in either month",
            plans=[],
            leaves_out=list(LIMITATIONS),
            estimate_note=ESTIMATE_NOTE,
        )
        return success(
            empty,
            provenance,
            total=0,
            shown=0,
            note=f"{name} is not on {where} in {args.from_month} or {args.to_month}, so there "
            "is no cost sharing to compare.",
        )
    tiers_before = {p.tier_before for p in plans}
    tiers_after = {p.tier_after for p in plans}
    tier_changed = any(p.tier_before != p.tier_after for p in plans)
    priced_plans = [p for p in plans if p.priced and p.change is not None]
    data = EstimateCostOutput(
        rxcui=args.rxcui,
        display_name=name,
        from_month=args.from_month,
        to_month=args.to_month,
        formulary_id=args.formulary_id,
        plan_id=plans[0].plan_id if args.plan_id else None,
        tier_before=next(iter(tiers_before)) if len(tiers_before) == 1 else None,
        tier_after=next(iter(tiers_after)) if len(tiers_after) == 1 else None,
        tier_changed=tier_changed,
        before=_span([p.before for p in plans]),
        after=_span([p.after for p in plans]),
        change=_span([p.change for p in priced_plans]),
        open_ended=open_ended,
        priced=bool(priced_plans),
        basis=bases[0] if bases else "no tier change, so the published cost sharing is the same",
        plans=plans,
        leaves_out=list(LIMITATIONS),
        estimate_note=ESTIMATE_NOTE,
    )
    notes = []
    if not tier_changed:
        notes.append(
            f"{name} did not change tier on {where} between {args.from_month} and "
            f"{args.to_month}, so the estimated change in cost sharing is zero. Other changes, "
            "such as a new prior authorization, do not show up in this figure."
        )
    if not priced_plans:
        notes.append(
            "Not priced: the plan does not publish dollar cost sharing for this tier, for "
            "example coinsurance with no dollar bounds."
        )
    if open_ended:
        notes.append(OPEN_ENDED_NOTE)
    return success(data, provenance, note=" ".join(notes) or None)
