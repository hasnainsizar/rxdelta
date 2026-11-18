"""count_changes: change counts computed in code, never by reading a list."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, Field

from rxdelta.tools.changes_core import (
    ChangeFilters,
    ChangeRow,
    all_rows,
    apply_filters,
    check_filters,
    empty_note,
)
from rxdelta.tools.context import ToolContext
from rxdelta.tools.envelope import ErrorCode, Provenance, ToolResult, cap_note, failure, success

NAME = "count_changes"
DESCRIPTION = (
    "Count coverage changes between two loaded months with the same filters as get_changes, "
    "grouped by change_type, formulary_id, rxcui or tier_move (for example '3->4'). Each "
    "group and the grand total give four counts: rows (drug, formulary and change type "
    "combinations), drugs (distinct RXCUIs), formularies and plans. Use this for any 'how "
    "many' question instead of counting a list. It does not list the drugs. "
    'Example input: {"from_month": "2026-01", "to_month": "2026-03", '
    '"change_types": ["tier_up"], "group_by": "change_type"}'
)
EXAMPLE: dict[str, Any] = {
    "from_month": "2026-01",
    "to_month": "2026-03",
    "change_types": ["tier_up"],
    "group_by": "change_type",
}

GroupBy = Literal["change_type", "formulary_id", "rxcui", "tier_move"]


class CountChangesInput(ChangeFilters):
    group_by: GroupBy = Field(default="change_type", description="How to group the counts.")


class Counts(BaseModel):
    rows: int
    drugs: int
    formularies: int
    plans: int


class CountGroup(BaseModel):
    key: str
    label: str
    counts: Counts


class CountChangesOutput(BaseModel):
    from_month: str
    to_month: str
    group_by: GroupBy
    groups: list[CountGroup]
    totals: Counts


def _tier(row: ChangeRow, side: str) -> str:
    coverage = row.before if side == "before" else row.after
    return "none" if coverage is None else str(coverage.tier)


def _group_key(row: ChangeRow, group_by: GroupBy) -> tuple[str, str]:
    if group_by == "change_type":
        return row.change_type.value, row.change_type.label
    if group_by == "formulary_id":
        return row.formulary_id, f"Formulary {row.formulary_id}"
    if group_by == "rxcui":
        return row.rxcui, row.display_name
    move = f"{_tier(row, 'before')}->{_tier(row, 'after')}"
    return move, f"Tier {move.replace('->', ' to ')}"


def _counts(rows: list[ChangeRow]) -> Counts:
    return Counts(
        rows=len(rows),
        drugs=len({r.rxcui for r in rows}),
        formularies=len({r.formulary_id for r in rows}),
        plans=len({p for r in rows for p in r.plans}),
    )


def count_changes(args: CountChangesInput, ctx: ToolContext) -> ToolResult[CountChangesOutput]:
    months = [args.from_month, args.to_month]
    provenance = Provenance(tool=NAME, args=args.model_dump(mode="json"), months=months)
    conn = ctx.connect()
    try:
        problem = check_filters(conn, args)
        if problem:
            return failure(problem[0], problem[1], provenance)
        rows = apply_filters(all_rows(conn, ctx.db_path, args.from_month, args.to_month), args)
    finally:
        conn.close()

    buckets: dict[tuple[str, str], list[ChangeRow]] = defaultdict(list)
    for row in rows:
        buckets[_group_key(row, args.group_by)].append(row)
    groups = sorted(
        (
            CountGroup(key=k, label=label, counts=_counts(members))
            for (k, label), members in buckets.items()
        ),
        key=lambda g: (-g.counts.rows, g.key),
    )
    cap = ctx.config.tools.cap("count_changes")
    shown = groups[:cap]
    provenance.row_keys = [k for r in rows for k in r.row_keys(*months)]
    data = CountChangesOutput(
        from_month=args.from_month,
        to_month=args.to_month,
        group_by=args.group_by,
        groups=shown,
        totals=_counts(rows),
    )
    if not rows:
        return failure(ErrorCode.NO_RESULTS, empty_note(args), provenance, data=data, total=0)
    note = None
    if len(groups) > len(shown):
        note = cap_note(
            len(groups), len(shown), "the totals still cover every group; narrow the filters"
        )
    return success(data, provenance, total=len(groups), shown=len(shown), note=note)
