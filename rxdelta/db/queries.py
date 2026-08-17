"""Read side of the database. Returns shared domain types, never raw rows,
so callers do not depend on the column layout."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from rxdelta.types import CostLeg, CostRow, DrugCoverage, PlanKey, PlanRecord

FACT_TABLES = ("formulary", "plan_info", "beneficiary_cost")

_CHANNELS = (
    ("retail_preferred", "pref"),
    ("retail_non_preferred", "nonpref"),
    ("mail_preferred", "mail_pref"),
    ("mail_non_preferred", "mail_nonpref"),
)


@dataclass(frozen=True)
class IngestLogEntry:
    snapshot_month: str
    file_name: str
    sha256: str
    row_count: int
    rejected_row_count: int
    loaded_at: str


def loaded_months(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT snapshot_month FROM ingest_log ORDER BY snapshot_month"
    ).fetchall()
    return [str(r["snapshot_month"]) for r in rows]


def month_is_loaded(conn: sqlite3.Connection, month: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ingest_log WHERE snapshot_month = ? LIMIT 1", (month,)
    ).fetchone()
    return row is not None


def row_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Rows per table per month, plus the rejected row count."""
    counts: dict[str, dict[str, int]] = {}
    for table in (*FACT_TABLES, "rejected_rows"):
        rows = conn.execute(
            f"SELECT snapshot_month, COUNT(*) AS n FROM {table} GROUP BY snapshot_month"  # noqa: S608
        ).fetchall()
        counts[table] = {str(r["snapshot_month"]): int(r["n"]) for r in rows}
    return counts


def ingest_log(conn: sqlite3.Connection) -> list[IngestLogEntry]:
    rows = conn.execute(
        "SELECT snapshot_month, file_name, sha256, row_count, rejected_row_count, loaded_at "
        "FROM ingest_log ORDER BY snapshot_month, file_name"
    ).fetchall()
    return [
        IngestLogEntry(
            snapshot_month=str(r["snapshot_month"]),
            file_name=str(r["file_name"]),
            sha256=str(r["sha256"]),
            row_count=int(r["row_count"]),
            rejected_row_count=int(r["rejected_row_count"]),
            loaded_at=str(r["loaded_at"]),
        )
        for r in rows
    ]


def rejection_reasons(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    rows = conn.execute(
        "SELECT snapshot_month, reason, COUNT(*) AS n FROM rejected_rows "
        "GROUP BY snapshot_month, reason ORDER BY snapshot_month, n DESC"
    ).fetchall()
    return [(str(r["snapshot_month"]), str(r["reason"]), int(r["n"])) for r in rows]


def plans(conn: sqlite3.Connection, month: str, contract_id: str | None = None) -> list[PlanRecord]:
    sql = (
        "SELECT contract_id, plan_id, segment_id, formulary_id, plan_name, contract_name "
        "FROM plan_info WHERE snapshot_month = ?"
    )
    params: list[str] = [month]
    if contract_id:
        sql += " AND contract_id = ?"
        params.append(contract_id)
    sql += " ORDER BY contract_id, plan_id, segment_id"
    return [
        PlanRecord(
            key=PlanKey(str(r["contract_id"]), str(r["plan_id"]), str(r["segment_id"])),
            formulary_id=str(r["formulary_id"]),
            plan_name=str(r["plan_name"]),
            contract_name=str(r["contract_name"]),
        )
        for r in conn.execute(sql, params).fetchall()
    ]


def formulary_coverage(
    conn: sqlite3.Connection, month: str, formulary_id: str
) -> dict[str, DrugCoverage]:
    rows = conn.execute(
        "SELECT ndc_11, rxcui, tier_level, prior_auth, step_therapy, quantity_limit, "
        "quantity_limit_amount, quantity_limit_days FROM formulary "
        "WHERE snapshot_month = ? AND formulary_id = ?",
        (month, formulary_id),
    ).fetchall()
    return {
        str(r["ndc_11"]): DrugCoverage(
            ndc_11=str(r["ndc_11"]),
            rxcui=str(r["rxcui"]),
            tier_level=int(r["tier_level"]),
            prior_auth=bool(r["prior_auth"]),
            step_therapy=bool(r["step_therapy"]),
            quantity_limit=bool(r["quantity_limit"]),
            quantity_limit_amount=(
                None if r["quantity_limit_amount"] is None else float(r["quantity_limit_amount"])
            ),
            quantity_limit_days=(
                None if r["quantity_limit_days"] is None else int(r["quantity_limit_days"])
            ),
        )
        for r in rows
    }


_COVERAGE_COLUMNS = (
    "ndc_11, rxcui, tier_level, prior_auth, step_therapy, "
    "quantity_limit, quantity_limit_amount, quantity_limit_days"
)

# Rows that cannot differ cannot produce a change. On the real files only 0.078
# percent of rows differ in place, so letting SQLite discard the rest avoids
# building two million objects the diff would immediately throw away. The
# comparison of what a difference *means* stays in the diff layer.
_CANDIDATES_SQL = f"""
SELECT {", ".join("a." + c for c in _COVERAGE_COLUMNS.split(", "))},
       {", ".join("b." + c + " AS b_" + c for c in _COVERAGE_COLUMNS.split(", "))}
  FROM formulary a
  LEFT JOIN formulary b
    ON b.snapshot_month = :month_to AND b.formulary_id = :formulary_to
   AND b.ndc_11 = a.ndc_11
 WHERE a.snapshot_month = :month_from AND a.formulary_id = :formulary_from
   AND (b.ndc_11 IS NULL
        OR a.tier_level <> b.tier_level
        OR a.prior_auth <> b.prior_auth
        OR a.step_therapy <> b.step_therapy
        OR a.quantity_limit <> b.quantity_limit
        OR IFNULL(a.quantity_limit_amount, -1) <> IFNULL(b.quantity_limit_amount, -1)
        OR IFNULL(a.quantity_limit_days, -1) <> IFNULL(b.quantity_limit_days, -1))
UNION ALL
SELECT {", ".join("NULL" for _ in _COVERAGE_COLUMNS.split(", "))},
       {", ".join("b." + c for c in _COVERAGE_COLUMNS.split(", "))}
  FROM formulary b
 WHERE b.snapshot_month = :month_to AND b.formulary_id = :formulary_to
   AND NOT EXISTS (
        SELECT 1 FROM formulary a
         WHERE a.snapshot_month = :month_from AND a.formulary_id = :formulary_from
           AND a.ndc_11 = b.ndc_11)
"""


def _coverage_from(row: sqlite3.Row, prefix: str = "") -> DrugCoverage | None:
    ndc = row[f"{prefix}ndc_11"]
    if ndc is None:
        return None
    amount = row[f"{prefix}quantity_limit_amount"]
    days = row[f"{prefix}quantity_limit_days"]
    return DrugCoverage(
        ndc_11=str(ndc),
        rxcui=str(row[f"{prefix}rxcui"]),
        tier_level=int(row[f"{prefix}tier_level"]),
        prior_auth=bool(row[f"{prefix}prior_auth"]),
        step_therapy=bool(row[f"{prefix}step_therapy"]),
        quantity_limit=bool(row[f"{prefix}quantity_limit"]),
        quantity_limit_amount=None if amount is None else float(amount),
        quantity_limit_days=None if days is None else int(days),
    )


def candidate_changes(
    conn: sqlite3.Connection,
    month_from: str,
    month_to: str,
    formulary_from: str,
    formulary_to: str,
) -> list[tuple[DrugCoverage | None, DrugCoverage | None]]:
    """Every (before, after) pair for one formulary pair that could be a change.

    Rows identical across the two months are filtered out in SQL. Everything
    else, including additions and removals, is returned for the diff layer to
    classify.
    """
    rows = conn.execute(
        _CANDIDATES_SQL,
        {
            "month_from": month_from,
            "month_to": month_to,
            "formulary_from": formulary_from,
            "formulary_to": formulary_to,
        },
    ).fetchall()
    return [(_coverage_from(r), _coverage_from(r, "b_")) for r in rows]


def cost_rows(
    conn: sqlite3.Connection, month: str, coverage_level: str
) -> dict[tuple[PlanKey, int], list[CostRow]]:
    """All cost rows for one coverage phase, indexed by plan and tier."""
    rows = conn.execute(
        "SELECT * FROM beneficiary_cost WHERE snapshot_month = ? AND coverage_level = ?",
        (month, coverage_level),
    ).fetchall()
    out: dict[tuple[PlanKey, int], list[CostRow]] = {}
    for r in rows:
        key = PlanKey(str(r["contract_id"]), str(r["plan_id"]), str(r["segment_id"]))
        legs = tuple(
            CostLeg(
                channel=channel,
                cost_type=str(r[f"cost_type_{suffix}"]),
                cost_amt=_maybe_float(r[f"cost_amt_{suffix}"]),
                cost_min_amt=_maybe_float(r[f"cost_min_amt_{suffix}"]),
                cost_max_amt=_maybe_float(r[f"cost_max_amt_{suffix}"]),
            )
            for channel, suffix in _CHANNELS
        )
        out.setdefault((key, int(r["tier"])), []).append(
            CostRow(
                key=key,
                coverage_level=str(r["coverage_level"]),
                tier=int(r["tier"]),
                days_supply=str(r["days_supply"]),
                legs=legs,
            )
        )
    return out


def drug_names(conn: sqlite3.Connection) -> dict[str, str]:
    """RXCUI to name for every cached entry. Reference data, no month filter."""
    rows = conn.execute("SELECT rxcui, name FROM drug_names WHERE name != ''").fetchall()
    return {str(r["rxcui"]): str(r["name"]) for r in rows}


def name_cache_size(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM drug_names WHERE name != ''").fetchone()
    return int(row["n"]) if row else 0


def distinct_ndc_count(conn: sqlite3.Connection, month: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT ndc_11) AS n FROM formulary WHERE snapshot_month = ?", (month,)
    ).fetchone()
    return int(row["n"]) if row else 0


def _maybe_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]
