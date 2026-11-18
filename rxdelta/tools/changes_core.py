"""Change rows for the change tools, built on the existing diff engine.

`diff_snapshots` classifies every drug and plan pair. This module rolls those up
to one row per RXCUI, formulary and change type, which is the grain a question
is asked at, and counts the plans each row reaches. Classification is never
redone here.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from rxdelta.ask.values import change_values
from rxdelta.config import Config
from rxdelta.db import queries
from rxdelta.diff.cost_direction import CostDirection, directions
from rxdelta.diff.engine import diff_snapshots
from rxdelta.tools.common import (
    CoverageValues,
    Month,
    Rxcui,
    ToolInput,
    display_names,
    formulary_key,
    month_problem,
    plan_text,
    rxcui_known,
)
from rxdelta.tools.envelope import ErrorCode
from rxdelta.types import ChangeType, PlanKey

_CHANGE_ORDER = {c: i for i, c in enumerate(ChangeType)}
# rxcui, formulary before, formulary after, change type, before value, after value,
# and the full before and after coverage, so rows that differ anywhere stay apart.
# and for a tier move its cost direction, since plans on one formulary pair can
# price the two tiers differently.
_RowKey = tuple[str, str, str, ChangeType, str | None, str | None, str, str, str]
_TIER_MOVES = frozenset({ChangeType.TIER_UP, ChangeType.TIER_DOWN})


class ChangeRow(BaseModel):
    rxcui: str
    display_name: str
    formulary_id: str = Field(description="Formulary the plans used in to_month.")
    formulary_id_from: str = Field(description="Formulary the plans used in from_month.")
    change_type: ChangeType
    before_value: str | None
    after_value: str | None
    cost_direction: CostDirection | None = Field(
        description="For a tier move, whether the member pays more, less or the same, read "
        "from the plan's published cost sharing; unknown when it cannot be told. "
        "None for other change types."
    )
    before: CoverageValues | None
    after: CoverageValues | None
    ndcs: list[str]
    affected_plans: int
    plans: list[str]

    def row_keys(self, month_from: str, month_to: str) -> list[str]:
        keys: list[str] = []
        for ndc in self.ndcs:
            if self.before is not None:
                keys.append(formulary_key(month_from, self.formulary_id_from, ndc))
            if self.after is not None:
                keys.append(formulary_key(month_to, self.formulary_id, ndc))
        return keys


class ChangeFilters(ToolInput):
    from_month: Month = Field(description="Baseline month, YYYY-MM.")
    to_month: Month = Field(description="Comparison month, YYYY-MM.")
    rxcuis: list[Rxcui] | None = Field(
        default=None, description="Only these RXCUIs. Use find_drug to get them."
    )
    change_types: list[ChangeType] | None = Field(
        default=None, description="Only these change types."
    )
    formulary_ids: list[str] | None = Field(
        default=None, description="Only these formulary IDs (matched in either month)."
    )
    tier_from: int | None = Field(default=None, ge=1, description="Tier before the change.")
    tier_to: int | None = Field(default=None, ge=1, description="Tier after the change.")

    @model_validator(mode="after")
    def _distinct_months(self) -> ChangeFilters:
        if self.from_month == self.to_month:
            raise ValueError("from_month and to_month must be different months")
        return self

    def describe(self) -> str:
        parts = [f"from {self.from_month} to {self.to_month}"]
        if self.change_types:
            parts.append("change type " + " or ".join(c.value for c in self.change_types))
        if self.formulary_ids:
            parts.append("formulary " + " or ".join(self.formulary_ids))
        if self.rxcuis:
            parts.append("RXCUI " + " or ".join(self.rxcuis))
        if self.tier_from is not None:
            parts.append(f"tier before {self.tier_from}")
        if self.tier_to is not None:
            parts.append(f"tier after {self.tier_to}")
        return ", ".join(parts)


_memo: dict[tuple[str, int, int, str, str], list[ChangeRow]] = {}
_memo_lock = threading.Lock()


def _memo_key(db_path: Path, month_from: str, month_to: str) -> tuple[str, int, int, str, str]:
    stat = db_path.stat()
    return (str(db_path.resolve()), stat.st_mtime_ns, stat.st_size, month_from, month_to)


def all_rows(
    conn: sqlite3.Connection, db_path: Path, config: Config, month_from: str, month_to: str
) -> list[ChangeRow]:
    """Every change row for the month pair, memoized per database file state."""
    key = _memo_key(db_path, month_from, month_to)
    with _memo_lock:
        hit = _memo.get(key)
        if hit is None:
            hit = _build_rows(conn, config, month_from, month_to)
            _memo[key] = hit
    return hit


def _build_rows(
    conn: sqlite3.Connection, config: Config, month_from: str, month_to: str
) -> list[ChangeRow]:
    result = diff_snapshots(conn, month_from, month_to)
    costs = directions(conn, config, result)
    formularies_from = {p.key: p.formulary_id for p in queries.plans(conn, month_from)}
    formularies_to = {p.key: p.formulary_id for p in queries.plans(conn, month_to)}
    plans: dict[_RowKey, set[PlanKey]] = defaultdict(set)
    ndcs: dict[_RowKey, set[str]] = defaultdict(set)
    states: dict[_RowKey, tuple[CoverageValues | None, CoverageValues | None]] = {}
    for change, cost in zip(result.changes, costs, strict=True):
        before = CoverageValues.from_coverage(change.before) if change.before else None
        after = CoverageValues.from_coverage(change.after) if change.after else None
        # A package code swap carries the old NDC before and the new one after.
        found_in = {c.ndc_11 for c in (change.before, change.after) if c is not None}
        for change_type in change.change_types:
            before_value, after_value = change_values(
                change_type,
                before.state() if before else None,
                after.state() if after else None,
            )
            key: _RowKey = (
                change.rxcui,
                formularies_from[change.plan],
                formularies_to[change.plan],
                change_type,
                before_value,
                after_value,
                before.model_dump_json() if before else "",
                after.model_dump_json() if after else "",
                cost.value if cost is not None and change_type in _TIER_MOVES else "",
            )
            plans[key].add(change.plan)
            ndcs[key] |= found_in
            states[key] = (before, after)

    names = display_names(conn, [k[0] for k in plans])
    rows = [
        ChangeRow(
            rxcui=key[0],
            display_name=names[key[0]],
            formulary_id=key[2],
            formulary_id_from=key[1],
            change_type=key[3],
            before_value=key[4],
            after_value=key[5],
            cost_direction=CostDirection(key[8]) if key[8] else None,
            before=states[key][0],
            after=states[key][1],
            ndcs=sorted(ndcs[key]),
            affected_plans=len(plans[key]),
            plans=[plan_text(p) for p in sorted(plans[key])],
        )
        for key in plans
    ]
    return sorted(rows, key=sort_key)


def sort_key(row: ChangeRow) -> tuple[str, str, str, int, str, str, str]:
    return (
        row.formulary_id,
        row.display_name.lower(),
        row.rxcui,
        _CHANGE_ORDER[row.change_type],
        row.before_value or "",
        row.after_value or "",
        row.cost_direction.value if row.cost_direction else "",
    )


def apply_filters(rows: list[ChangeRow], filters: ChangeFilters) -> list[ChangeRow]:
    rxcuis = set(filters.rxcuis or [])
    types = set(filters.change_types or [])
    formularies = set(filters.formulary_ids or [])
    out = []
    for row in rows:
        if rxcuis and row.rxcui not in rxcuis:
            continue
        if types and row.change_type not in types:
            continue
        if formularies and not ({row.formulary_id, row.formulary_id_from} & formularies):
            continue
        if filters.tier_from is not None and (
            row.before is None or row.before.tier != filters.tier_from
        ):
            continue
        if filters.tier_to is not None and (row.after is None or row.after.tier != filters.tier_to):
            continue
        out.append(row)
    return out


def check_filters(conn: sqlite3.Connection, filters: ChangeFilters) -> tuple[ErrorCode, str] | None:
    """Problems with otherwise valid filters, as an error code and a message."""
    problem = month_problem(conn, filters.from_month, filters.to_month)
    if problem:
        return ErrorCode.MONTH_NOT_LOADED, problem
    unknown = [r for r in filters.rxcuis or [] if not rxcui_known(conn, r)]
    if unknown:
        return (
            ErrorCode.UNKNOWN_DRUG,
            f"RXCUI {', '.join(unknown)} is not in any loaded formulary or name source. "
            "Use find_drug to look a drug up by name.",
        )
    absent = [
        f
        for f in filters.formulary_ids or []
        if not conn.execute(
            "SELECT 1 FROM plan_info WHERE formulary_id = ? AND snapshot_month IN (?, ?) LIMIT 1",
            (f, filters.from_month, filters.to_month),
        ).fetchone()
    ]
    if absent:
        return (
            ErrorCode.UNKNOWN_PLAN,
            f"Formulary {', '.join(absent)} is not used by any plan in {filters.from_month} or "
            f"{filters.to_month}. Use find_plan to look formulary IDs up.",
        )
    return None


def empty_note(filters: ChangeFilters) -> str:
    return (
        f"No changes match: {filters.describe()}. The months are loaded and were compared; "
        "nothing of that kind changed."
    )
