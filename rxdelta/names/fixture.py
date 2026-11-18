"""Load names from a local CSV of rxcui, name_type, name rows (source fixture)."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import cast

from rxdelta.names.store import NAME_TYPES, NameRow, NameType, upsert
from rxdelta.types import RxdeltaError

SOURCE = "fixture"
# Fixed so a database built from fixtures is identical on every run.
FIXTURE_STAMP = "2026-01-01T00:00:00+00:00"


def read_rows(path: Path) -> list[NameRow]:
    if not path.is_file():
        raise RxdeltaError(f"Names file not found: {path}")
    rows: list[NameRow] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for line_number, raw in enumerate(csv.DictReader(handle), start=2):
            name_type = (raw.get("name_type") or "").strip()
            if name_type not in NAME_TYPES:
                raise RxdeltaError(
                    f"{path.name} line {line_number}: name_type must be one of "
                    f"{', '.join(NAME_TYPES)}, got {name_type!r}"
                )
            rows.append(
                NameRow(
                    rxcui=(raw.get("rxcui") or "").strip(),
                    name=(raw.get("name") or "").strip(),
                    name_type=cast(NameType, name_type),
                    source=SOURCE,
                    fetched_at=FIXTURE_STAMP,
                )
            )
    return rows


def load(conn: sqlite3.Connection, path: Path) -> int:
    return upsert(conn, read_rows(path))
