"""In-place migrations for databases created by an earlier version."""

from __future__ import annotations

import sqlite3

from rxdelta.names.store import NameRow, rows_from_full_name

_NEW_DRUG_NAMES = """
CREATE TABLE drug_names (
    rxcui      TEXT NOT NULL,
    name       TEXT NOT NULL,
    name_type  TEXT NOT NULL CHECK (name_type IN ('full', 'ingredient', 'brand')),
    name_norm  TEXT NOT NULL,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (rxcui, name_type, name_norm)
)
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate_drug_names(conn: sqlite3.Connection) -> bool:
    """Move a one-name-per-RXCUI table to the multi-name shape.

    Each old row becomes a full name, plus a brand row when the name carries
    one. Runs in one transaction and is a no-op on a new or migrated database.
    Returns True when a migration ran.
    """
    columns = _columns(conn, "drug_names")
    if not columns or "name_type" in columns:
        return False
    old = conn.execute("SELECT rxcui, name, source, fetched_at FROM drug_names").fetchall()
    rows: list[NameRow] = []
    for rxcui, name, source, fetched_at in old:
        if str(name).strip():
            rows.extend(rows_from_full_name(str(rxcui), str(name), str(source), str(fetched_at)))
    with conn:
        conn.execute("DROP TABLE drug_names")
        conn.execute(_NEW_DRUG_NAMES)
        conn.executemany(
            "INSERT OR IGNORE INTO drug_names "
            "(rxcui, name, name_type, name_norm, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(r.rxcui, r.name, r.name_type, r.name_norm, r.source, r.fetched_at) for r in rows],
        )
    return True
