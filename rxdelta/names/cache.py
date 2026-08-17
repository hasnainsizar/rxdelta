"""The committed name cache: a small CSV, and how it reaches the database.

The report never fetches. It reads the table, which is populated from this file
on load, so a checkout with no network still renders drug names.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

FIELDNAMES = ("rxcui", "name", "source", "fetched_at")


@dataclass(frozen=True)
class DrugName:
    rxcui: str
    name: str
    source: str
    fetched_at: str


def read_csv(path: Path) -> dict[str, DrugName]:
    """Read the cache. A missing file is an empty cache, not an error: the
    report falls back to the NDC and says so."""
    if not path.is_file():
        return {}
    out: dict[str, DrugName] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            rxcui = (row.get("rxcui") or "").strip()
            name = (row.get("name") or "").strip()
            if not rxcui:
                continue
            # A row with no name records an RXCUI RxNav could not resolve. It is
            # kept so a later refresh does not ask again, and dropped on the way
            # into the database so the report falls back to the NDC.
            out[rxcui] = DrugName(
                rxcui=rxcui,
                name=name,
                source=(row.get("source") or "").strip(),
                fetched_at=(row.get("fetched_at") or "").strip(),
            )
    return out


def write_csv(path: Path, names: dict[str, DrugName]) -> int:
    """Write the cache sorted by RXCUI so repeat runs produce no git churn."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        for rxcui in sorted(names, key=_sort_key):
            entry = names[rxcui]
            writer.writerow(
                {
                    "rxcui": entry.rxcui,
                    "name": entry.name,
                    "source": entry.source,
                    "fetched_at": entry.fetched_at,
                }
            )
    return len(names)


def _sort_key(rxcui: str) -> tuple[int, int | str]:
    return (0, int(rxcui)) if rxcui.isdigit() else (1, rxcui)


def merge_into_db(conn: sqlite3.Connection, names: dict[str, DrugName]) -> int:
    """Upsert names into the reference table. Idempotent by RXCUI."""
    resolved = [n for n in names.values() if n.name]
    if not resolved:
        return 0
    with conn:
        conn.executemany(
            "INSERT INTO drug_names (rxcui, name, source, fetched_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(rxcui) DO UPDATE SET "
            "name = excluded.name, source = excluded.source, fetched_at = excluded.fetched_at",
            [(n.rxcui, n.name, n.source, n.fetched_at) for n in resolved],
        )
    return len(resolved)


def load_cache(conn: sqlite3.Connection, path: Path) -> int:
    """Load the committed cache into the database. Called on every report and
    diff so a fresh database still has names without a refresh."""
    return merge_into_db(conn, read_csv(path))
