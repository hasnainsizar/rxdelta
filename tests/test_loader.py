from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from rxdelta.config import Config
from rxdelta.db import queries
from rxdelta.ingest.loader import load_month, validate_month
from rxdelta.types import LoadError, SchemaError
from tests.conftest import BAD_SCHEMA, SNAPSHOTS

TABLES = ("formulary", "plan_info", "beneficiary_cost", "rejected_rows")


def snapshot_state(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    state = {}
    for table in TABLES:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()  # noqa: S608
        state[table] = [tuple(r) for r in rows]
    return state


def test_load_writes_expected_row_counts(conn: sqlite3.Connection, config: Config) -> None:
    summary = load_month(conn, config, "2024-01", SNAPSHOTS)
    by_type = {f.file_type: f for f in summary.files}
    assert by_type["formulary"].row_count == 17
    assert by_type["plan_info"].row_count == 2
    assert by_type["beneficiary_cost"].row_count == 10
    assert summary.total_rejected == 0


def test_load_is_idempotent(conn: sqlite3.Connection, config: Config) -> None:
    load_month(conn, config, "2024-01", SNAPSHOTS)
    first = snapshot_state(conn)
    first_log = queries.ingest_log(conn)

    load_month(conn, config, "2024-01", SNAPSHOTS)
    second = snapshot_state(conn)
    second_log = queries.ingest_log(conn)

    assert first == second
    assert [(e.file_name, e.sha256, e.row_count) for e in first_log] == [
        (e.file_name, e.sha256, e.row_count) for e in second_log
    ]
    assert len(second_log) == 3


def test_loading_a_second_month_leaves_the_first_alone(
    conn: sqlite3.Connection, config: Config
) -> None:
    load_month(conn, config, "2024-01", SNAPSHOTS)
    before = conn.execute(
        "SELECT COUNT(*) FROM formulary WHERE snapshot_month = '2024-01'"
    ).fetchone()[0]
    load_month(conn, config, "2024-02", SNAPSHOTS)
    after = conn.execute(
        "SELECT COUNT(*) FROM formulary WHERE snapshot_month = '2024-01'"
    ).fetchone()[0]
    assert before == after
    assert queries.loaded_months(conn) == ["2024-01", "2024-02"]


def test_ndc_raw_is_kept_alongside_the_normalized_form(
    conn: sqlite3.Connection, config: Config
) -> None:
    load_month(conn, config, "2024-01", SNAPSHOTS)
    row = conn.execute(
        "SELECT ndc_11, ndc_raw FROM formulary WHERE ndc_raw = '1111-1111-15'"
    ).fetchone()
    assert row["ndc_11"] == "01111111115"
    assert row["ndc_raw"] == "1111-1111-15"


def test_schema_mismatch_names_missing_and_unexpected_columns(
    conn: sqlite3.Connection, config: Config
) -> None:
    with pytest.raises(SchemaError) as exc_info:
        load_month(conn, config, "2024-01", BAD_SCHEMA)
    message = str(exc_info.value)
    assert "Missing columns (1): FORMULARY_ID" in message
    assert "Unexpected columns (2): FORM_ID, SURPRISE_COLUMN" in message
    assert "basic drugs formulary file.txt" in message
    assert "month 2024-01" in message


def test_schema_mismatch_writes_nothing(conn: sqlite3.Connection, config: Config) -> None:
    with pytest.raises(SchemaError):
        load_month(conn, config, "2024-01", BAD_SCHEMA)
    assert queries.loaded_months(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM formulary").fetchone()[0] == 0


def test_ambiguous_ndc_lands_in_rejected_rows(
    conn: sqlite3.Connection, lenient_config: Config, tmp_path: Path
) -> None:
    root = tmp_path / "snapshots"
    shutil.copytree(SNAPSHOTS / "2024-01", root / "2024-01")
    target = root / "2024-01" / "basic drugs formulary file.txt"
    target.write_text(
        target.read_text() + "F0001|1|2024|100099|1234567890|2|N|||N|N|N\n", encoding="utf-8"
    )

    summary = load_month(conn, lenient_config, "2024-01", root)
    assert summary.total_rejected == 1
    row = conn.execute("SELECT * FROM rejected_rows").fetchone()
    assert row["file_name"] == "basic drugs formulary file.txt"
    assert row["line_number"] == 19
    assert "ambiguous" in row["reason"]
    assert row["raw_value"] == "1234567890"


def test_load_fails_above_the_rejected_row_threshold(
    conn: sqlite3.Connection, config: Config, tmp_path: Path
) -> None:
    root = tmp_path / "snapshots"
    shutil.copytree(SNAPSHOTS / "2024-01", root / "2024-01")
    target = root / "2024-01" / "basic drugs formulary file.txt"
    extra = "".join(
        f"F0001|1|2024|10{i:04d}|123456789{i % 10}|2|N|||N|N|N\n" for i in range(200, 210)
    )
    target.write_text(target.read_text() + extra, encoding="utf-8")

    with pytest.raises(LoadError, match="above the 5.00% limit"):
        load_month(conn, config, "2024-01", root)
    assert conn.execute("SELECT COUNT(*) FROM formulary").fetchone()[0] == 0


def test_missing_source_file_names_the_patterns(
    conn: sqlite3.Connection, config: Config, tmp_path: Path
) -> None:
    root = tmp_path / "snapshots"
    shutil.copytree(SNAPSHOTS / "2024-01", root / "2024-01")
    (root / "2024-01" / "plan information.txt").unlink()
    with pytest.raises(LoadError, match="plan_info"):
        load_month(conn, config, "2024-01", root)


def test_missing_month_directory_is_explicit(
    conn: sqlite3.Connection, config: Config, tmp_path: Path
) -> None:
    with pytest.raises(LoadError, match="No snapshot directory for 2024-07"):
        load_month(conn, config, "2024-07", tmp_path)


@pytest.mark.parametrize("month", ["2024", "2024-13", "2024-1", "jan-2024", "2024-00"])
def test_month_format_is_validated(month: str) -> None:
    with pytest.raises(LoadError, match="YYYY-MM"):
        validate_month(month)
