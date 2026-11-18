"""get_changes: what changed between two months, one row per drug, formulary and type."""

from __future__ import annotations

from typing import Any

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

NAME = "get_changes"
DESCRIPTION = (
    "List coverage changes between two loaded months, classified by the rxdelta diff: tier "
    "up or down, prior authorization, step therapy or quantity limit added or removed, "
    "quantity limit tightened or loosened, drug added to or removed from a formulary. One row "
    "per RXCUI, formulary and change type, with before and after values and how many plans it "
    "affects. Filter by rxcuis, change_types, formulary_ids, tier_from and tier_to; page with "
    "limit and offset. It does not estimate cost (use estimate_cost_change) and does not count "
    "reliably past the row cap (use count_changes for counts). "
    'Example input: {"from_month": "2026-01", "to_month": "2026-03", '
    '"change_types": ["prior_auth_added"], "formulary_ids": ["00026123"]}'
)
EXAMPLE: dict[str, Any] = {
    "from_month": "2026-01",
    "to_month": "2026-03",
    "change_types": ["prior_auth_added"],
    "formulary_ids": ["00026123"],
}


class GetChangesInput(ChangeFilters):
    limit: int | None = Field(default=None, ge=1, description="Rows per page.")
    offset: int = Field(default=0, ge=0, description="Rows to skip, for paging.")


class GetChangesOutput(BaseModel):
    from_month: str
    to_month: str
    offset: int
    rows: list[ChangeRow]


def get_changes(args: GetChangesInput, ctx: ToolContext) -> ToolResult[GetChangesOutput]:
    months = [args.from_month, args.to_month]
    provenance = Provenance(tool=NAME, args=args.model_dump(mode="json"), months=months)
    conn = ctx.connect()
    try:
        problem = check_filters(conn, args)
        if problem:
            return failure(problem[0], problem[1], provenance)
        rows = apply_filters(
            all_rows(conn, ctx.db_path, ctx.config, args.from_month, args.to_month), args
        )
    finally:
        conn.close()

    cap = ctx.config.tools.cap("get_changes")
    page = min(args.limit or ctx.config.tools.default_page_size, cap)
    shown = rows[args.offset : args.offset + page]
    provenance.row_keys = [k for r in shown for k in r.row_keys(*months)]
    data = GetChangesOutput(
        from_month=args.from_month, to_month=args.to_month, offset=args.offset, rows=shown
    )
    if not rows:
        return success(data, provenance, total=0, shown=0, note=empty_note(args))
    if not shown:
        return failure(
            ErrorCode.INVALID_ARGS,
            f"offset {args.offset} is past the end; there are {len(rows)} matching rows.",
            provenance,
            data=data,
            total=len(rows),
        )
    note = None
    if len(rows) > len(shown):
        note = cap_note(
            len(rows),
            len(shown),
            f"rows {args.offset + 1} to {args.offset + len(shown)} are shown; narrow with "
            "formulary_ids, change_types or rxcuis, page with offset, or use count_changes "
            "for totals",
        )
    return success(data, provenance, total=len(rows), shown=len(shown), note=note)
