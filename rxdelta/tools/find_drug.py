"""find_drug: turn a drug name, misspelling, RXCUI or NDC into RXCUIs."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

from rxdelta.names.match import DrugMatch, DrugMatcher, MatchType
from rxdelta.tools.common import ToolInput
from rxdelta.tools.context import ToolContext
from rxdelta.tools.envelope import ErrorCode, Provenance, ToolResult, cap_note, failure, success

NAME = "find_drug"
DESCRIPTION = (
    "Find drugs by brand name, ingredient name, misspelled name, RXCUI or NDC, and return "
    "their RXCUIs grouped by product. A product such as one brand can have several RXCUIs "
    "(different strengths or forms); 'ambiguous' is true when more than one RXCUI matched. "
    "Each RXCUI says whether it is on any loaded formulary. It does not say anything about "
    'coverage or changes. Example input: {"query": "ozempic", "limit": 10}'
)
EXAMPLE: dict[str, Any] = {"query": "ozempic", "limit": 10}


class FindDrugInput(ToolInput):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    limit: int = Field(default=10, ge=1, description="Most RXCUIs to return.")


class DrugHit(BaseModel):
    rxcui: str
    display_name: str
    matched_on: str
    match_type: MatchType
    score: float
    on_formulary: bool
    months: list[str]


class ProductHit(BaseModel):
    product: str
    rxcuis: list[str]
    match_type: MatchType
    score: float
    drugs: list[DrugHit]


class NearMiss(BaseModel):
    rxcui: str
    name: str
    score: float


class FindDrugOutput(BaseModel):
    query: str
    products: list[ProductHit]
    ambiguous: bool
    near_misses: list[NearMiss]


def _months_by_rxcui(ctx: ToolContext, rxcuis: list[str]) -> dict[str, list[str]]:
    conn = ctx.connect()
    try:
        out: dict[str, list[str]] = {r: [] for r in rxcuis}
        for rxcui in rxcuis:
            rows = conn.execute(
                "SELECT DISTINCT snapshot_month FROM formulary WHERE rxcui = ? "
                "ORDER BY snapshot_month",
                (rxcui,),
            ).fetchall()
            out[rxcui] = [str(r[0]) for r in rows]
        return out
    finally:
        conn.close()


def _near(matches: list[DrugMatch]) -> list[NearMiss]:
    return [NearMiss(rxcui=m.rxcui, name=m.matched_on, score=m.score) for m in matches]


def find_drug(args: FindDrugInput, ctx: ToolContext) -> ToolResult[FindDrugOutput]:
    limit = min(args.limit, ctx.config.tools.cap("find_drug"))
    conn = ctx.connect()
    try:
        matcher = DrugMatcher(conn, ctx.config.match)
        matches = matcher.match_all(args.query)
        shown = matches[:limit]
        products = matcher.group(shown)
        misses = [] if matches else matcher.near_misses(args.query)
    finally:
        conn.close()
    months = _months_by_rxcui(ctx, [m.rxcui for m in shown])
    hits = [
        ProductHit(
            product=p.product,
            rxcuis=p.rxcuis,
            match_type=p.match_type,
            score=p.score,
            drugs=[
                DrugHit(
                    **m.model_dump(),
                    on_formulary=bool(months[m.rxcui]),
                    months=months[m.rxcui],
                )
                for m in p.matches
            ],
        )
        for p in products
    ]
    all_months = sorted({month for found in months.values() for month in found})
    provenance = Provenance(
        tool=NAME,
        args=args.model_dump(mode="json"),
        months=all_months,
        row_keys=[f"drug_names:{m.rxcui}" for m in shown],
    )
    data = FindDrugOutput(
        query=args.query, products=hits, ambiguous=len(shown) > 1, near_misses=_near(misses)
    )
    if not matches:
        message = f"No drug matches {args.query!r}."
        if misses:
            listed = ", ".join(f"{m.matched_on} (score {m.score:g})" for m in misses)
            message += f" Closest names below the match threshold: {listed}."
        else:
            message += " Nothing in the name table is close to it."
        return failure(ErrorCode.UNKNOWN_DRUG, message, provenance, data=data, total=0)

    notes = []
    if len(hits) > 1:
        notes.append(
            f"{args.query!r} matches {len(hits)} different products. Ask which one is meant, "
            "or answer for each."
        )
    elif len(shown) > 1:
        notes.append(
            f"{args.query!r} is one product with {len(shown)} RXCUIs (different strengths or "
            "forms). Ask which one is meant, or answer for each."
        )
    off = [d.rxcui for p in hits for d in p.drugs if not d.on_formulary]
    if off:
        notes.append(f"Not on any loaded formulary: RXCUI {', '.join(off)}.")
    if len(matches) > len(shown):
        notes.append(cap_note(len(matches), len(shown), "use a more specific name or a strength"))
    return success(
        data,
        provenance,
        total=len(matches),
        shown=len(shown),
        note=" ".join(notes) or None,
    )
