"""Load names from a local RxNorm RXNCONSO.RRF file instead of the API.

RXNCONSO carries concept names but not the links from a drug to its
ingredients, which live in RXNREL. So this loader stores the full name of each
formulary RXCUI plus the brand embedded in branded names. Ingredient queries
still match through the token stage, since the ingredient is part of the name.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rxdelta.names.store import formulary_rxcuis, log_missing, rows_from_full_name, upsert
from rxdelta.types import RxdeltaError

SOURCE = "rrf"
# Field positions in RXNCONSO.RRF, from the RxNorm technical documentation.
_RXCUI, _LAT, _SAB, _TTY, _STR, _SUPPRESS = 0, 1, 11, 12, 14, 16
# Term types that name the concept itself, most specific first.
_PREFERRED_TTY = ("SBD", "SCD", "BPCK", "GPCK", "SBDC", "SCDC", "SBDF", "SCDF", "BN", "IN", "MIN")


@dataclass(frozen=True)
class RrfSummary:
    in_scope: int
    named: int
    not_found: int


def _best_names(path: Path, wanted: set[str]) -> dict[str, str]:
    best: dict[str, tuple[int, str]] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            fields = line.rstrip("\n").split("|")
            if len(fields) <= _SUPPRESS or fields[_RXCUI] not in wanted:
                continue
            if fields[_SAB] != "RXNORM" or fields[_LAT] != "ENG" or fields[_SUPPRESS] == "O":
                continue
            tty = fields[_TTY]
            if tty not in _PREFERRED_TTY:
                continue
            rank = _PREFERRED_TTY.index(tty)
            current = best.get(fields[_RXCUI])
            if current is None or rank < current[0]:
                best[fields[_RXCUI]] = (rank, fields[_STR])
    return {rxcui: name for rxcui, (_, name) in best.items()}


def load(conn: sqlite3.Connection, path: Path) -> RrfSummary:
    if not path.is_file():
        raise RxdeltaError(f"RRF file not found: {path}")
    wanted = set(formulary_rxcuis(conn))
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    names = _best_names(path, wanted)
    upsert(
        conn,
        [
            row
            for rxcui, name in names.items()
            for row in rows_from_full_name(rxcui, name, SOURCE, stamp)
        ],
    )
    for rxcui in sorted(wanted - set(names)):
        log_missing(conn, rxcui, SOURCE, "no RXNORM English name in the RRF file", stamp)
    return RrfSummary(in_scope=len(wanted), named=len(names), not_found=len(wanted - set(names)))
