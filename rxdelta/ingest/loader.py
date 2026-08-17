"""Write one monthly snapshot into SQLite.

Loading is a delete-then-insert of the month partition inside a single
transaction, so running the same month twice leaves identical table contents.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rxdelta.config import Config
from rxdelta.ingest import reader, schema
from rxdelta.ingest.normalize import (
    NdcResult,
    normalize_ndc,
    normalize_plan_key,
    parse_code,
    parse_flag,
    parse_int,
    parse_optional_float,
    parse_optional_int,
)
from rxdelta.types import LoadError

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

_COST_CHANNEL_SUFFIXES = ("PREF", "NONPREF", "MAIL_PREF", "MAIL_NONPREF")
_PARTITIONED_TABLES = ("formulary", "plan_info", "beneficiary_cost", "rejected_rows", "ingest_log")


@dataclass
class FileResult:
    file_type: str
    file_name: str
    sha256: str
    row_count: int
    rejected_row_count: int
    collapsed_row_count: int = 0


@dataclass
class LoadSummary:
    month: str
    directory: Path
    files: list[FileResult] = field(default_factory=list)
    rejected_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return sum(f.row_count for f in self.files)

    @property
    def total_rejected(self) -> int:
        return sum(f.rejected_row_count for f in self.files)

    @property
    def total_collapsed(self) -> int:
        return sum(f.collapsed_row_count for f in self.files)


def validate_month(month: str) -> str:
    if not MONTH_PATTERN.match(month):
        raise LoadError(f"Month must be formatted YYYY-MM, got {month!r}")
    return month


def load_month(
    conn: sqlite3.Connection, config: Config, month: str, directory: Path
) -> LoadSummary:
    validate_month(month)
    source_dir = reader.month_dir(directory, month)
    files = reader.discover(config, source_dir)

    summary = LoadSummary(month=month, directory=source_dir)
    loaded_at = datetime.now(UTC).isoformat(timespec="seconds")

    # One transaction for the whole month. Rows stream into it in chunks rather
    # than being accumulated in memory first, and any failure, including the
    # rejected row threshold, rolls the whole month back. "Nothing was written"
    # still holds.
    try:
        with conn:
            for table in _PARTITIONED_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE snapshot_month = ?", (month,))  # noqa: S608

            for file_type in (schema.FORMULARY, schema.PLAN_INFO, schema.BENEFICIARY_COST):
                source = files[file_type]
                header = reader.read_header(config, source.path)
                schema.validate_columns(file_type, header, file_name=source.name, month=month)
                result = _stream_file(conn, config, month, source)

                # Collapsed repeats are not candidates: they carry no new
                # information, so they neither pass nor fail. The threshold
                # measures real losses.
                total = result.row_count + result.rejected_row_count
                _enforce_reject_threshold(
                    config, source.name, month, result.rejected_row_count, total
                )
                summary.files.append(result)

            conn.executemany(
                "INSERT INTO ingest_log "
                "(snapshot_month, file_name, sha256, row_count, rejected_row_count, loaded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (month, f.file_name, f.sha256, f.row_count, f.rejected_row_count, loaded_at)
                    for f in summary.files
                ],
            )
            for reason, count in _reason_counts(conn, month).items():
                summary.rejected_reasons[reason] = count
    except sqlite3.DatabaseError as exc:
        raise LoadError(f"Writing month {month} failed and was rolled back: {exc}") from exc
    return summary


def _reason_counts(conn: sqlite3.Connection, month: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT reason, COUNT(*) AS n FROM rejected_rows WHERE snapshot_month = ? GROUP BY reason",
        (month,),
    ).fetchall()
    return {str(r["reason"]): int(r["n"]) for r in rows}


def _enforce_reject_threshold(
    config: Config, file_name: str, month: str, rejected: int, total: int
) -> None:
    if total == 0:
        raise LoadError(f"{file_name} (month {month}) contains no data rows")
    pct = 100.0 * rejected / total
    if pct > config.ingest.max_rejected_pct:
        raise LoadError(
            f"{file_name} (month {month}) rejected {rejected} of {total} rows ({pct:.2f}%), "
            f"above the {config.ingest.max_rejected_pct:.2f}% limit in [ingest].max_rejected_pct. "
            "Nothing was written."
        )


@dataclass(frozen=True)
class _RowContext:
    """What every row parser may need: config, the version key, and the file
    name that goes into an error message."""

    config: Config
    month: str
    file_name: str


def _stream_file(
    conn: sqlite3.Connection, config: Config, month: str, source: reader.SourceFile
) -> FileResult:
    """Parse one file straight into the open transaction, a chunk at a time."""
    context = _RowContext(config=config, month=month, file_name=source.name)
    parser = {
        schema.FORMULARY: _parse_formulary_row,
        schema.PLAN_INFO: _parse_plan_info_row,
        schema.BENEFICIARY_COST: _parse_cost_row,
    }[source.file_type]
    insert = {
        schema.FORMULARY: _FORMULARY_INSERT,
        schema.PLAN_INFO: _PLAN_INFO_INSERT,
        schema.BENEFICIARY_COST: _COST_INSERT,
    }[source.file_type]
    chunk_size = config.ingest.insert_chunk_rows

    pending: list[tuple[Any, ...]] = []
    rejects: list[tuple[Any, ...]] = []
    written = rejected = collapsed = 0
    seen: dict[tuple[Any, ...], tuple[Any, ...]] = {}

    def flush() -> None:
        nonlocal written, rejected
        if pending:
            conn.executemany(insert, pending)
            written += len(pending)
            pending.clear()
        if rejects:
            conn.executemany(
                "INSERT INTO rejected_rows "
                "(snapshot_month, file_name, line_number, reason, raw_value) "
                "VALUES (?, ?, ?, ?, ?)",
                rejects,
            )
            rejected += len(rejects)
            rejects.clear()

    def reject(line_number: int, reason: str, raw: str) -> None:
        rejects.append((month, source.name, line_number, reason, raw))

    for parsed in reader.read_rows(config, source.path):
        count_error = reader.field_count_error(parsed.values)
        if count_error:
            reject(parsed.line_number, count_error, "")
            continue
        try:
            row = parser(context, parsed.values)
        except ValueError as exc:
            reject(parsed.line_number, str(exc), _preview(parsed.values))
            continue
        values, reject_reason, raw = row
        if reject_reason is not None:
            reject(parsed.line_number, reject_reason, raw)
            continue
        assert values is not None
        key = _primary_key(source.file_type, values)
        previous = seen.get(key)
        if previous is not None:
            # The CMS plan information file carries one row per plan per county,
            # so a plan appears once for every county it is offered in. Our grain
            # is the plan, and the county columns are not stored, so identical
            # repeats collapse. A repeat that disagrees on a stored value is a
            # real conflict and still fails.
            if previous == values:
                collapsed += 1
                continue
            reject(
                parsed.line_number,
                "duplicate primary key with conflicting values in source file",
                raw,
            )
            continue
        seen[key] = values
        pending.append(values)
        if len(pending) >= chunk_size or len(rejects) >= chunk_size:
            flush()
    flush()
    return FileResult(
        file_type=source.file_type,
        file_name=source.name,
        sha256=reader.sha256(source.path),
        row_count=written,
        rejected_row_count=rejected,
        collapsed_row_count=collapsed,
    )


_ParsedOutcome = tuple[tuple[Any, ...] | None, str | None, str]


def _parse_formulary_row(context: _RowContext, values: dict[str, str]) -> _ParsedOutcome:
    ndc: NdcResult = normalize_ndc(
        values["NDC"], unhyphenated_10_policy=context.config.ingest.ndc.unhyphenated_10_digit
    )
    if not ndc.ok:
        return None, ndc.reason, ndc.raw
    assert ndc.ndc_11 is not None
    row = (
        context.month,
        parse_code(values["FORMULARY_ID"], field="FORMULARY_ID"),
        ndc.ndc_11,
        ndc.raw.strip(),
        values["RXCUI"].strip(),
        parse_int(values["TIER_LEVEL_VALUE"], field="TIER_LEVEL_VALUE"),
        int(parse_flag(values["PRIOR_AUTHORIZATION_YN"], field="PRIOR_AUTHORIZATION_YN")),
        int(parse_flag(values["STEP_THERAPY_YN"], field="STEP_THERAPY_YN")),
        int(parse_flag(values["QUANTITY_LIMIT_YN"], field="QUANTITY_LIMIT_YN")),
        parse_optional_float(values["QUANTITY_LIMIT_AMOUNT"]),
        parse_optional_int(values["QUANTITY_LIMIT_DAYS"]),
    )
    return row, None, ndc.raw


def _parse_plan_info_row(context: _RowContext, values: dict[str, str]) -> _ParsedOutcome:
    key = normalize_plan_key(values["CONTRACT_ID"], values["PLAN_ID"], values["SEGMENT_ID"])
    if not key.contract_id:
        return None, "CONTRACT_ID is required and was empty", ""
    row = (
        context.month,
        key.contract_id,
        key.plan_id,
        key.segment_id,
        parse_code(values["FORMULARY_ID"], field="FORMULARY_ID"),
        values["PLAN_NAME"].strip(),
        values["CONTRACT_NAME"].strip(),
    )
    return row, None, str(key)


def _parse_cost_row(context: _RowContext, values: dict[str, str]) -> _ParsedOutcome:
    key = normalize_plan_key(values["CONTRACT_ID"], values["PLAN_ID"], values["SEGMENT_ID"])
    coverage_level = parse_code(values["COVERAGE_LEVEL"], field="COVERAGE_LEVEL")
    context.config.codes.coverage_level_name(coverage_level, context.file_name)
    days_supply = parse_code(values["DAYS_SUPPLY"], field="DAYS_SUPPLY")
    context.config.codes.days_for(days_supply, context.file_name)

    cost_fields: list[Any] = []
    for suffix in _COST_CHANNEL_SUFFIXES:
        cost_type = values[f"COST_TYPE_{suffix}"].strip()
        if cost_type:
            context.config.codes.cost_type_code(cost_type, context.file_name)
        cost_fields.extend(
            [
                cost_type,
                parse_optional_float(values[f"COST_AMT_{suffix}"]),
                parse_optional_float(values[f"COST_MIN_AMT_{suffix}"]),
                parse_optional_float(values[f"COST_MAX_AMT_{suffix}"]),
            ]
        )

    row = (
        context.month,
        key.contract_id,
        key.plan_id,
        key.segment_id,
        coverage_level,
        parse_int(values["TIER"], field="TIER"),
        days_supply,
        *cost_fields,
    )
    return row, None, str(key)


def _primary_key(file_type: str, values: tuple[Any, ...]) -> tuple[Any, ...]:
    if file_type == schema.FORMULARY:
        return values[0], values[1], values[2]
    if file_type == schema.PLAN_INFO:
        return values[0], values[1], values[2], values[3]
    return values[0], values[1], values[2], values[3], values[4], values[5], values[6]


def _preview(values: dict[str, str]) -> str:
    return "|".join(list(values.values())[:6])[:200]


_FORMULARY_INSERT = """
INSERT INTO formulary (
    snapshot_month, formulary_id, ndc_11, ndc_raw, rxcui, tier_level,
    prior_auth, step_therapy, quantity_limit, quantity_limit_amount, quantity_limit_days
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_PLAN_INFO_INSERT = """
INSERT INTO plan_info (
    snapshot_month, contract_id, plan_id, segment_id, formulary_id, plan_name, contract_name
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_COST_INSERT = """
INSERT INTO beneficiary_cost (
    snapshot_month, contract_id, plan_id, segment_id, coverage_level, tier, days_supply,
    cost_type_pref, cost_amt_pref, cost_min_amt_pref, cost_max_amt_pref,
    cost_type_nonpref, cost_amt_nonpref, cost_min_amt_nonpref, cost_max_amt_nonpref,
    cost_type_mail_pref, cost_amt_mail_pref, cost_min_amt_mail_pref, cost_max_amt_mail_pref,
    cost_type_mail_nonpref, cost_amt_mail_nonpref, cost_min_amt_mail_nonpref,
    cost_max_amt_mail_nonpref
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
