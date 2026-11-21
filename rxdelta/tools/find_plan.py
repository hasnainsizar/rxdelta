"""find_plan: look up plans by plan ID, contract ID, formulary ID or name."""

from __future__ import annotations

import re
import sqlite3
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

from rxdelta.db.queries import loaded_months
from rxdelta.names.normalize import normalize_name
from rxdelta.tools.common import Month, ToolInput, month_problem, parse_plan_id
from rxdelta.tools.context import ToolContext
from rxdelta.tools.envelope import ErrorCode, Provenance, ToolResult, cap_note, failure, success

NAME = "find_plan"
DESCRIPTION = (
    "Find Medicare Part D plans by contract-plan ID (H1234-001 or H1234-001-000), contract ID "
    "(H1234), formulary ID (00026123) or part of the plan or company name, in one month "
    "(default: the latest loaded). Returns each plan's ID, segment, name and the formulary it "
    "uses. It does not describe coverage or changes. "
    'Example input: {"query": "H1234-001", "month": "2026-03"}'
)
EXAMPLE: dict[str, Any] = {"query": "H1234-001", "month": "2026-03"}

_CONTRACT = re.compile(r"^[A-Z]\d{4}$")
_FORMULARY = re.compile(r"^\d{5,10}$")


class FindPlanInput(ToolInput):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    month: Month | None = Field(default=None, description="Snapshot month, YYYY-MM.")


class PlanHit(BaseModel):
    plan_id: str
    contract_id: str
    plan_number: str
    segment_id: str
    plan_name: str
    contract_name: str
    formulary_id: str


class FindPlanOutput(BaseModel):
    query: str
    month: str | None
    matched_by: Literal["plan_id", "contract_id", "formulary_id", "name", "none"]
    plans: list[PlanHit]


def _hits(rows: list[sqlite3.Row]) -> list[PlanHit]:
    return [
        PlanHit(
            plan_id=f"{r['contract_id']}-{r['plan_id']}-{r['segment_id']}",
            contract_id=str(r["contract_id"]),
            plan_number=str(r["plan_id"]),
            segment_id=str(r["segment_id"]),
            plan_name=str(r["plan_name"]),
            contract_name=str(r["contract_name"]),
            formulary_id=str(r["formulary_id"]),
        )
        for r in rows
    ]


def _search(conn: sqlite3.Connection, query: str, month: str) -> tuple[str, list[sqlite3.Row]]:
    base = (
        "SELECT contract_id, plan_id, segment_id, plan_name, contract_name, formulary_id "
        "FROM plan_info WHERE snapshot_month = ?"
    )
    order = " ORDER BY contract_id, plan_id, segment_id"
    text = query.strip().upper()
    key = parse_plan_id(text)
    if key is not None:
        rows = conn.execute(
            base + " AND contract_id = ? AND plan_id = ? AND segment_id = ?" + order,
            (month, key.contract_id, key.plan_id, key.segment_id),
        ).fetchall()
        return "plan_id", rows
    if _CONTRACT.match(text):
        return "contract_id", conn.execute(
            base + " AND contract_id = ?" + order, (month, text)
        ).fetchall()
    if _FORMULARY.match(text):
        return "formulary_id", conn.execute(
            base + " AND formulary_id = ?" + order, (month, text)
        ).fetchall()
    wanted = set(normalize_name(query).split())
    rows = [
        r
        for r in conn.execute(base + order, (month,)).fetchall()
        if wanted
        and wanted <= set(normalize_name(f"{r['plan_name']} {r['contract_name']}").split())
    ]
    return "name", rows


def find_plan(args: FindPlanInput, ctx: ToolContext) -> ToolResult[FindPlanOutput]:
    conn = ctx.connect()
    try:
        months = loaded_months(conn)
        month = args.month or (months[-1] if months else None)
        provenance = Provenance(tool=NAME, args=args.model_dump(mode="json"))
        if month is None:
            return failure(
                ErrorCode.MONTH_NOT_LOADED,
                "No months are loaded, so there are no plans to search.",
                provenance,
            )
        problem = month_problem(conn, month)
        if problem:
            return failure(ErrorCode.MONTH_NOT_LOADED, problem, provenance)
        matched_by, rows = _search(conn, args.query, month)
    finally:
        conn.close()

    cap = ctx.config.tools.cap("find_plan")
    hits = _hits(rows)
    shown = hits[:cap]
    provenance = Provenance(
        tool=NAME,
        args=args.model_dump(mode="json"),
        months=[month],
        row_keys=[f"plan_info:{month}:{h.plan_id}" for h in shown],
    )
    data = FindPlanOutput(
        query=args.query,
        month=month,
        matched_by=matched_by if hits else "none",  # type: ignore[arg-type]
        plans=shown,
    )
    if not hits:
        return failure(
            ErrorCode.UNKNOWN_PLAN,
            f"No plan matches {args.query!r} in {month}. Plan IDs look like H1234-001; "
            "formulary IDs are 8 digits; names can be partial.",
            provenance,
            data=data,
            total=0,
        )
    note = None
    if len(hits) > len(shown):
        note = cap_note(len(hits), len(shown), "pass a full plan ID or more of the name")
    return success(data, provenance, total=len(hits), shown=len(shown), note=note)
