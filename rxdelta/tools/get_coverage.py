"""get_coverage: how formularies cover one drug in one month."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel, Field, model_validator

from rxdelta.tools.common import (
    CoverageValues,
    FormularyId,
    Month,
    PlanId,
    Rxcui,
    ToolInput,
    display_names,
    formulary_exists,
    formulary_key,
    month_problem,
    parse_plan_id,
    plan_formulary,
    plan_text,
    rxcui_known,
)
from rxdelta.tools.context import ToolContext
from rxdelta.tools.envelope import ErrorCode, Provenance, ToolResult, cap_note, failure, success

NAME = "get_coverage"
DESCRIPTION = (
    "Show how one drug (by RXCUI) is covered in one month: tier, prior authorization, step "
    "therapy and quantity limit (amount per days), per formulary. Narrow it with formulary_id "
    "or plan_id (not both). Without either, it returns a summary of how many formularies "
    "cover the drug and their tier spread, plus capped detail rows. It does not compare "
    "months; use get_changes for that. Get RXCUIs from find_drug. "
    'Example input: {"rxcui": "8800141", "month": "2026-03", '
    '"formulary_id": "00026123"}'
)
EXAMPLE: dict[str, Any] = {"rxcui": "8800141", "month": "2026-03", "formulary_id": "00026123"}


class GetCoverageInput(ToolInput):
    rxcui: Rxcui = Field(description="RXCUI from find_drug.")
    month: Month = Field(description="Snapshot month, YYYY-MM.")
    formulary_id: FormularyId | None = Field(default=None, description="One formulary.")
    plan_id: PlanId | None = Field(
        default=None, description="One plan, H1234-001 or H1234-001-000."
    )

    @model_validator(mode="after")
    def _one_scope(self) -> GetCoverageInput:
        if self.formulary_id and self.plan_id:
            raise ValueError("pass formulary_id or plan_id, not both")
        return self


class CoverageRow(BaseModel):
    formulary_id: str
    ndcs: list[str]
    coverage: CoverageValues
    plans: int


class CoverageSummary(BaseModel):
    formularies_covering: int
    formularies_in_month: int
    tier_distribution: dict[str, int]


class GetCoverageOutput(BaseModel):
    rxcui: str
    display_name: str
    month: str
    formulary_id: str | None
    plan_id: str | None
    rows: list[CoverageRow]
    summary: CoverageSummary


def get_coverage(args: GetCoverageInput, ctx: ToolContext) -> ToolResult[GetCoverageOutput]:
    conn = ctx.connect()
    try:
        provenance = Provenance(tool=NAME, args=args.model_dump(mode="json"), months=[args.month])
        problem = month_problem(conn, args.month)
        if problem:
            return failure(ErrorCode.MONTH_NOT_LOADED, problem, provenance)
        if not rxcui_known(conn, args.rxcui):
            return failure(
                ErrorCode.UNKNOWN_DRUG,
                f"RXCUI {args.rxcui} is not in any loaded formulary or name source. "
                "Use find_drug to look a drug up by name.",
                provenance,
            )
        formulary_id = args.formulary_id
        plan_label = None
        if args.plan_id:
            key = parse_plan_id(args.plan_id)
            assert key is not None
            plan_label = plan_text(key)
            formulary_id = plan_formulary(conn, key, args.month)
            if formulary_id is None:
                return failure(
                    ErrorCode.UNKNOWN_PLAN,
                    f"Plan {plan_label} is not in the {args.month} plan file. Use find_plan.",
                    provenance,
                )
        elif formulary_id and not formulary_exists(conn, formulary_id, args.month):
            return failure(
                ErrorCode.UNKNOWN_PLAN,
                f"Formulary {formulary_id} is not in the {args.month} formulary file. "
                "Use find_plan to look formulary IDs up.",
                provenance,
            )

        sql = (
            "SELECT formulary_id, ndc_11, tier_level, prior_auth, step_therapy, quantity_limit, "
            "quantity_limit_amount, quantity_limit_days FROM formulary "
            "WHERE snapshot_month = ? AND rxcui = ?"
        )
        params: list[str] = [args.month, args.rxcui]
        if formulary_id:
            sql += " AND formulary_id = ?"
            params.append(formulary_id)
        rows = conn.execute(sql + " ORDER BY formulary_id, ndc_11", params).fetchall()
        plan_counts = dict(
            conn.execute(
                "SELECT formulary_id, COUNT(*) FROM plan_info WHERE snapshot_month = ? "
                "GROUP BY formulary_id",
                (args.month,),
            ).fetchall()
        )
        in_month = conn.execute(
            "SELECT COUNT(DISTINCT formulary_id) FROM formulary WHERE snapshot_month = ?",
            (args.month,),
        ).fetchone()[0]
        name = display_names(conn, [args.rxcui])[args.rxcui]
    finally:
        conn.close()

    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    values: dict[tuple[str, str], CoverageValues] = {}
    for row in rows:
        coverage = CoverageValues.from_row(row)
        key2 = (str(row["formulary_id"]), coverage.model_dump_json())
        grouped[key2].append(str(row["ndc_11"]))
        values[key2] = coverage
    detail = [
        CoverageRow(
            formulary_id=f,
            ndcs=ndcs,
            coverage=values[(f, v)],
            plans=int(plan_counts.get(f, 0)),
        )
        for (f, v), ndcs in grouped.items()
    ]
    cap = ctx.config.tools.cap("get_coverage")
    shown = detail[:cap]
    tiers = Counter(str(r.coverage.tier) for r in detail)
    summary = CoverageSummary(
        formularies_covering=len({r.formulary_id for r in detail}),
        formularies_in_month=int(in_month),
        tier_distribution=dict(sorted(tiers.items())),
    )
    provenance = Provenance(
        tool=NAME,
        args=args.model_dump(mode="json"),
        months=[args.month],
        row_keys=[formulary_key(args.month, r.formulary_id, n) for r in shown for n in r.ndcs],
    )
    data = GetCoverageOutput(
        rxcui=args.rxcui,
        display_name=name,
        month=args.month,
        formulary_id=formulary_id,
        plan_id=plan_label,
        rows=shown,
        summary=summary,
    )
    if not detail:
        where = f"formulary {formulary_id}" if formulary_id else "any loaded formulary"
        if plan_label:
            where += f" (used by plan {plan_label})"
        return failure(
            ErrorCode.NO_RESULTS,
            f"{name} is not on {where} in {args.month}, so it is not covered there.",
            provenance,
            data=data,
            total=0,
        )
    note = None
    if len(detail) > len(shown):
        note = cap_note(len(detail), len(shown), "pass formulary_id or plan_id")
    return success(data, provenance, total=len(detail), shown=len(shown), note=note)
