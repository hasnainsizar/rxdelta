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
    "cover the drug and their tier spread, plus capped detail rows. To check several drugs on "
    "one formulary or plan in one call, pass rxcuis (a list) instead of rxcui, with "
    "formulary_id or plan_id; each drug comes back in 'drugs' with covered true or false. It "
    "does not compare months; use get_changes for that. Get RXCUIs from find_drug. "
    'Example input: {"rxcui": "8800141", "month": "2026-03", '
    '"formulary_id": "00026123"}'
)
EXAMPLE: dict[str, Any] = {"rxcui": "8800141", "month": "2026-03", "formulary_id": "00026123"}


class GetCoverageInput(ToolInput):
    rxcui: Rxcui | None = Field(default=None, description="RXCUI from find_drug.")
    rxcuis: list[Rxcui] | None = Field(
        default=None,
        min_length=1,
        description="Several RXCUIs on one formulary or plan, instead of rxcui.",
    )
    month: Month = Field(description="Snapshot month, YYYY-MM.")
    formulary_id: FormularyId | None = Field(default=None, description="One formulary.")
    plan_id: PlanId | None = Field(
        default=None, description="One plan, H1234-001 or H1234-001-000."
    )

    @model_validator(mode="after")
    def _one_scope(self) -> GetCoverageInput:
        if self.formulary_id and self.plan_id:
            raise ValueError("pass formulary_id or plan_id, not both")
        if (self.rxcui is None) == (self.rxcuis is None):
            raise ValueError("pass rxcui or rxcuis, exactly one of them")
        if self.rxcuis is not None and not (self.formulary_id or self.plan_id):
            raise ValueError("rxcuis needs formulary_id or plan_id")
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


class DrugCoverage(BaseModel):
    rxcui: str
    display_name: str
    covered: bool
    rows: list[CoverageRow]


class GetCoverageOutput(BaseModel):
    # rxcui, display_name and summary describe a single drug call; drugs a batch call.
    rxcui: str | None
    display_name: str | None
    month: str
    formulary_id: str | None
    plan_id: str | None
    rows: list[CoverageRow]
    summary: CoverageSummary | None
    drugs: list[DrugCoverage] | None = None


def _coverage_rows(rows: list[Any], plan_counts: dict[str, int]) -> list[CoverageRow]:
    """One row per formulary and distinct coverage, listing the NDCs that share it."""
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    values: dict[tuple[str, str], CoverageValues] = {}
    for row in rows:
        coverage = CoverageValues.from_row(row)
        key = (str(row["formulary_id"]), coverage.model_dump_json())
        grouped[key].append(str(row["ndc_11"]))
        values[key] = coverage
    return [
        CoverageRow(
            formulary_id=f,
            ndcs=ndcs,
            coverage=values[(f, v)],
            plans=int(plan_counts.get(f, 0)),
        )
        for (f, v), ndcs in grouped.items()
    ]


_SELECT = (
    "SELECT rxcui, formulary_id, ndc_11, tier_level, prior_auth, step_therapy, quantity_limit, "
    "quantity_limit_amount, quantity_limit_days FROM formulary WHERE snapshot_month = ?"
)


def _plan_counts(conn: Any, month: str) -> dict[str, int]:
    return dict(
        conn.execute(
            "SELECT formulary_id, COUNT(*) FROM plan_info WHERE snapshot_month = ? "
            "GROUP BY formulary_id",
            (month,),
        ).fetchall()
    )


def get_coverage(args: GetCoverageInput, ctx: ToolContext) -> ToolResult[GetCoverageOutput]:
    conn = ctx.connect()
    try:
        provenance = Provenance(tool=NAME, args=args.model_dump(mode="json"), months=[args.month])
        problem = month_problem(conn, args.month)
        if problem:
            return failure(ErrorCode.MONTH_NOT_LOADED, problem, provenance)
        wanted = list(dict.fromkeys(args.rxcuis or [args.rxcui or ""]))
        cap = ctx.config.tools.cap("get_coverage_rxcuis")
        if len(wanted) > cap:
            return failure(
                ErrorCode.INVALID_ARGS,
                f"At most {cap} RXCUIs per call; {len(wanted)} were passed. Split the list.",
                provenance,
            )
        unknown = [r for r in wanted if not rxcui_known(conn, r)]
        if unknown:
            return failure(
                ErrorCode.UNKNOWN_DRUG,
                f"RXCUI {', '.join(unknown)} is not in any loaded formulary or name source. "
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
        if args.rxcuis is not None:
            assert formulary_id is not None
            return _batch(conn, args, wanted, formulary_id, plan_label)
        assert args.rxcui is not None
        rxcui = args.rxcui

        sql = _SELECT + " AND rxcui = ?"
        params: list[str] = [args.month, rxcui]
        if formulary_id:
            sql += " AND formulary_id = ?"
            params.append(formulary_id)
        rows = conn.execute(sql + " ORDER BY formulary_id, ndc_11", params).fetchall()
        plan_counts = _plan_counts(conn, args.month)
        in_month = conn.execute(
            "SELECT COUNT(DISTINCT formulary_id) FROM formulary WHERE snapshot_month = ?",
            (args.month,),
        ).fetchone()[0]
        name = display_names(conn, [rxcui])[rxcui]
    finally:
        conn.close()

    detail = _coverage_rows(rows, plan_counts)
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
        rxcui=rxcui,
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
        return success(
            data,
            provenance,
            total=0,
            shown=0,
            note=f"{name} is not on {where} in {args.month}, so it is not covered there.",
        )
    note = None
    if len(detail) > len(shown):
        note = cap_note(len(detail), len(shown), "pass formulary_id or plan_id")
    return success(data, provenance, total=len(detail), shown=len(shown), note=note)


def _batch(
    conn: Any,
    args: GetCoverageInput,
    wanted: list[str],
    formulary_id: str,
    plan_label: str | None,
) -> ToolResult[GetCoverageOutput]:
    """Several drugs on one formulary in one month, in one query."""
    marks = ", ".join("?" for _ in wanted)
    rows = conn.execute(
        _SELECT + f" AND formulary_id = ? AND rxcui IN ({marks}) ORDER BY rxcui, ndc_11",  # noqa: S608
        [args.month, formulary_id, *wanted],
    ).fetchall()
    plan_counts = _plan_counts(conn, args.month)
    names = display_names(conn, wanted)
    by_drug: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        by_drug[str(row["rxcui"])].append(row)
    drugs = [
        DrugCoverage(
            rxcui=r,
            display_name=names[r],
            covered=bool(by_drug[r]),
            rows=_coverage_rows(by_drug[r], plan_counts),
        )
        for r in wanted
    ]
    provenance = Provenance(
        tool=NAME,
        args=args.model_dump(mode="json"),
        months=[args.month],
        row_keys=[
            formulary_key(args.month, formulary_id, n)
            for d in drugs
            for row in d.rows
            for n in row.ndcs
        ],
    )
    data = GetCoverageOutput(
        rxcui=None,
        display_name=None,
        month=args.month,
        formulary_id=formulary_id,
        plan_id=plan_label,
        rows=[],
        summary=None,
        drugs=drugs,
    )
    missing = [d.display_name for d in drugs if not d.covered]
    where = f"formulary {formulary_id}" + (f" (used by plan {plan_label})" if plan_label else "")
    note = (
        f"{len(missing)} of {len(drugs)} drugs are not on {where} in {args.month}: "
        + "; ".join(missing)
        + "."
        if missing
        else None
    )
    return success(data, provenance, total=len(drugs), shown=len(drugs), note=note)
