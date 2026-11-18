"""list_months: which snapshot months are loaded."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from rxdelta.tools.common import ToolInput, loaded_months
from rxdelta.tools.context import ToolContext
from rxdelta.tools.envelope import ErrorCode, Provenance, ToolResult, failure, success

NAME = "list_months"
DESCRIPTION = (
    "List the monthly formulary snapshots that are loaded, oldest first, with the earliest and "
    "latest month and how many formularies, plans and drugs each holds. Use it to resolve "
    "phrases like 'the latest month'. It does not compare months or describe changes. "
    "Example input: {}"
)
EXAMPLE: dict[str, Any] = {}


class ListMonthsInput(ToolInput):
    pass


class MonthInfo(BaseModel):
    month: str
    formularies: int
    plans: int
    drugs: int


class ListMonthsOutput(BaseModel):
    months: list[MonthInfo]
    earliest: str | None
    latest: str | None


def list_months(args: ListMonthsInput, ctx: ToolContext) -> ToolResult[ListMonthsOutput]:
    conn = ctx.connect()
    try:
        months = loaded_months(conn)
        info = []
        for month in months:
            row = conn.execute(
                "SELECT COUNT(DISTINCT formulary_id), COUNT(DISTINCT rxcui) FROM formulary "
                "WHERE snapshot_month = ?",
                (month,),
            ).fetchone()
            plans = conn.execute(
                "SELECT COUNT(*) FROM plan_info WHERE snapshot_month = ?", (month,)
            ).fetchone()
            info.append(MonthInfo(month=month, formularies=row[0], plans=plans[0], drugs=row[1]))
    finally:
        conn.close()
    provenance = Provenance(
        tool=NAME,
        args=args.model_dump(mode="json"),
        months=months,
        row_keys=[f"ingest_log:{m}" for m in months],
    )
    data = ListMonthsOutput(
        months=info,
        earliest=months[0] if months else None,
        latest=months[-1] if months else None,
    )
    if not months:
        return failure(
            ErrorCode.NO_RESULTS,
            "No months are loaded, so there is nothing to answer from. Load one with "
            "rxdelta load --month YYYY-MM.",
            provenance,
            data=data,
            total=0,
        )
    return success(data, provenance, total=len(months), shown=len(months))
