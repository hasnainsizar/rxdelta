"""Reads and writes for the drug_names and drug_names_missing tables."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from rxdelta.names.normalize import bracket_brand, normalize_name

NameType = Literal["full", "ingredient", "brand"]
NAME_TYPES: tuple[NameType, ...] = ("full", "ingredient", "brand")


@dataclass(frozen=True)
class NameRow:
    rxcui: str
    name: str
    name_type: NameType
    source: str
    fetched_at: str

    @property
    def name_norm(self) -> str:
        return normalize_name(self.name)


@dataclass(frozen=True)
class NameStatus:
    formulary_rxcuis: int
    named: int
    missing: int
    logged_not_found: int
    by_source: dict[str, int]


def rows_from_full_name(rxcui: str, name: str, source: str, fetched_at: str) -> list[NameRow]:
    """A full name, plus the brand RxNorm embeds in it when there is one."""
    rows = [NameRow(rxcui, name, "full", source, fetched_at)]
    brand = bracket_brand(name)
    if brand:
        rows.append(NameRow(rxcui, brand, "brand", source, fetched_at))
    return rows


def upsert(conn: sqlite3.Connection, rows: Iterable[NameRow]) -> int:
    """Insert or refresh rows. An RXCUI keeps at most one full name: a new full
    name replaces the old one rather than sitting beside it."""
    batch = [r for r in rows if r.name.strip() and r.name_norm]
    if not batch:
        return 0
    with conn:
        for row in batch:
            if row.name_type == "full":
                conn.execute(
                    "DELETE FROM drug_names WHERE rxcui = ? AND name_type = 'full' "
                    "AND name_norm != ?",
                    (row.rxcui, row.name_norm),
                )
        conn.executemany(
            "INSERT INTO drug_names (rxcui, name, name_type, name_norm, source, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(rxcui, name_type, name_norm) DO UPDATE SET "
            "name = excluded.name, source = excluded.source, fetched_at = excluded.fetched_at",
            [(r.rxcui, r.name, r.name_type, r.name_norm, r.source, r.fetched_at) for r in batch],
        )
        conn.execute(
            f"DELETE FROM drug_names_missing WHERE rxcui IN ({','.join('?' for _ in batch)})",  # noqa: S608
            [r.rxcui for r in batch],
        )
    return len(batch)


def log_missing(
    conn: sqlite3.Connection, rxcui: str, source: str, reason: str, checked_at: str
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO drug_names_missing (rxcui, source, reason, checked_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(rxcui) DO UPDATE SET "
            "source = excluded.source, reason = excluded.reason, checked_at = excluded.checked_at",
            (rxcui, source, reason, checked_at),
        )


def formulary_rxcuis(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT rxcui FROM formulary WHERE rxcui != '' ORDER BY rxcui"
    ).fetchall()
    return [str(r[0]) for r in rows]


def named_rxcuis(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT DISTINCT rxcui FROM drug_names").fetchall()}


def missing_rxcuis(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT rxcui FROM drug_names_missing").fetchall()
    return {str(r[0]) for r in rows}


def status(conn: sqlite3.Connection) -> NameStatus:
    in_formulary = set(formulary_rxcuis(conn))
    named = named_rxcuis(conn) & in_formulary
    by_source: dict[str, int] = {}
    for row in conn.execute(
        "SELECT source, COUNT(DISTINCT n.rxcui) FROM drug_names n "
        "WHERE n.rxcui IN (SELECT rxcui FROM formulary) GROUP BY source ORDER BY source"
    ).fetchall():
        by_source[str(row[0])] = int(row[1])
    return NameStatus(
        formulary_rxcuis=len(in_formulary),
        named=len(named),
        missing=len(in_formulary - named),
        logged_not_found=len(missing_rxcuis(conn) & in_formulary),
        by_source=by_source,
    )
