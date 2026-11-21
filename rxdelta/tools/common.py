"""Argument types and lookups shared by the tools."""

from __future__ import annotations

import re
import sqlite3
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from rxdelta.ask.values import CoverageState
from rxdelta.db.queries import loaded_months
from rxdelta.types import DrugCoverage, PlanKey

Month = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
Rxcui = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{1,8}$")]
FormularyId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16)]
PlanId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]\d{4}-\d{1,3}(-\d{1,3})?$"
    ),
]

_PLAN_ID = re.compile(r"^([A-Z]\d{4})-(\d{1,3})(?:-(\d{1,3}))?$")


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CoverageValues(BaseModel):
    tier: int
    prior_auth: bool
    step_therapy: bool
    quantity_limit: bool
    quantity_limit_amount: float | None
    quantity_limit_days: int | None

    @classmethod
    def from_coverage(cls, coverage: DrugCoverage) -> CoverageValues:
        return cls(
            tier=coverage.tier_level,
            prior_auth=coverage.prior_auth,
            step_therapy=coverage.step_therapy,
            quantity_limit=coverage.quantity_limit,
            quantity_limit_amount=coverage.quantity_limit_amount,
            quantity_limit_days=coverage.quantity_limit_days,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> CoverageValues:
        amount = row["quantity_limit_amount"]
        days = row["quantity_limit_days"]
        return cls(
            tier=int(row["tier_level"]),
            prior_auth=bool(row["prior_auth"]),
            step_therapy=bool(row["step_therapy"]),
            quantity_limit=bool(row["quantity_limit"]),
            quantity_limit_amount=None if amount is None else float(amount),
            quantity_limit_days=None if days is None else int(days),
        )

    def state(self) -> CoverageState:
        return CoverageState(
            tier=self.tier,
            prior_auth=self.prior_auth,
            step_therapy=self.step_therapy,
            quantity_limit=self.quantity_limit,
            quantity_limit_amount=self.quantity_limit_amount,
            quantity_limit_days=self.quantity_limit_days,
        )


def month_problem(conn: sqlite3.Connection, *months: str) -> str | None:
    """A plain message naming any month that is not loaded, or None."""
    loaded = loaded_months(conn)
    missing = [m for m in dict.fromkeys(months) if m not in loaded]
    if not missing:
        return None
    have = ", ".join(loaded) if loaded else "none"
    names = ", ".join(missing)
    verb = "is" if len(missing) == 1 else "are"
    return f"Month {names} {verb} not loaded. Loaded months: {have}."


def display_names(conn: sqlite3.Connection, rxcuis: list[str]) -> dict[str, str]:
    if not rxcuis:
        return {}
    unique = sorted(set(rxcuis))
    found: dict[str, str] = {}
    for start in range(0, len(unique), 500):
        chunk = unique[start : start + 500]
        rows = conn.execute(
            "SELECT rxcui, name FROM drug_names WHERE name_type = 'full' AND rxcui IN "
            f"({','.join('?' for _ in chunk)})",  # noqa: S608
            chunk,
        ).fetchall()
        found.update({str(r[0]): str(r[1]) for r in rows})
    return {r: found.get(r, f"RXCUI {r}") for r in unique}


def rxcui_known(conn: sqlite3.Connection, rxcui: str) -> bool:
    for sql in (
        "SELECT 1 FROM formulary WHERE rxcui = ? LIMIT 1",
        "SELECT 1 FROM drug_names WHERE rxcui = ? LIMIT 1",
    ):
        if conn.execute(sql, (rxcui,)).fetchone():
            return True
    return False


def parse_plan_id(text: str) -> PlanKey | None:
    found = _PLAN_ID.match(text.strip().upper())
    if not found:
        return None
    contract, plan, segment = found.groups()
    return PlanKey(contract, plan.zfill(3), (segment or "0").zfill(3))


def plan_text(key: PlanKey) -> str:
    return f"{key.contract_id}-{key.plan_id}-{key.segment_id}"


def plan_formulary(conn: sqlite3.Connection, key: PlanKey, month: str) -> str | None:
    row = conn.execute(
        "SELECT formulary_id FROM plan_info WHERE snapshot_month = ? AND contract_id = ? "
        "AND plan_id = ? AND segment_id = ?",
        (month, key.contract_id, key.plan_id, key.segment_id),
    ).fetchone()
    return None if row is None else str(row[0])


def formulary_exists(conn: sqlite3.Connection, formulary_id: str, month: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM formulary WHERE snapshot_month = ? AND formulary_id = ? LIMIT 1",
        (month, formulary_id),
    ).fetchone()
    return row is not None


def formulary_key(month: str, formulary_id: str, ndc_11: str) -> str:
    return f"formulary:{month}:{formulary_id}:{ndc_11}"
