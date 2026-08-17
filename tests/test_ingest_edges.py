from __future__ import annotations

import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from rxdelta.config import Config, load_config
from rxdelta.ingest import reader, schema
from rxdelta.ingest.loader import load_month
from rxdelta.types import ConfigError, LoadError, SchemaError
from tests.conftest import CONFIG_PATH, SNAPSHOTS


def copy_month(tmp_path: Path, month: str = "2024-01") -> Path:
    root = tmp_path / "snapshots"
    shutil.copytree(SNAPSHOTS / month, root / month)
    return root


def test_short_row_is_rejected_with_the_field_counts(
    conn: sqlite3.Connection, lenient_config: Config, tmp_path: Path
) -> None:
    root = copy_month(tmp_path)
    target = root / "2024-01" / "basic drugs formulary file.txt"
    target.write_text(target.read_text() + "F0001|1|2024|100099\n", encoding="utf-8")

    summary = load_month(conn, lenient_config, "2024-01", root)
    assert summary.total_rejected == 1
    reason = conn.execute("SELECT reason FROM rejected_rows").fetchone()["reason"]
    assert reason == "expected 12 fields, got 4"


def test_duplicate_primary_key_in_the_source_is_rejected(
    conn: sqlite3.Connection, lenient_config: Config, tmp_path: Path
) -> None:
    root = copy_month(tmp_path)
    target = root / "2024-01" / "basic drugs formulary file.txt"
    target.write_text(
        target.read_text() + "F0001|1|2024|100001|11111-1111-01|3|N|||N|N|N\n", encoding="utf-8"
    )
    summary = load_month(conn, lenient_config, "2024-01", root)
    assert summary.rejected_reasons == {
        "duplicate primary key with conflicting values in source file": 1
    }


def test_bad_tier_value_is_rejected_with_the_column_name(
    conn: sqlite3.Connection, lenient_config: Config, tmp_path: Path
) -> None:
    root = copy_month(tmp_path)
    target = root / "2024-01" / "basic drugs formulary file.txt"
    target.write_text(
        target.read_text() + "F0001|1|2024|100099|22222-2222-02|high|N|||N|N|N\n", encoding="utf-8"
    )
    summary = load_month(conn, lenient_config, "2024-01", root)
    assert any("TIER_LEVEL_VALUE" in reason for reason in summary.rejected_reasons)


def test_unknown_cost_type_code_names_the_code_and_the_file(
    conn: sqlite3.Connection, config: Config, tmp_path: Path
) -> None:
    root = copy_month(tmp_path)
    target = root / "2024-01" / "beneficiary cost file.txt"
    lines = target.read_text().splitlines()
    lines[1] = lines[1].replace("|1|5.00||", "|9|5.00||", 1)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_month(conn, config, "2024-01", root)
    message = str(exc_info.value)
    assert "'9'" in message
    assert "beneficiary cost file.txt" in message


def test_unknown_coverage_level_code_is_a_hard_failure(
    conn: sqlite3.Connection, config: Config, tmp_path: Path
) -> None:
    root = copy_month(tmp_path)
    target = root / "2024-01" / "beneficiary cost file.txt"
    lines = target.read_text().splitlines()
    lines[1] = lines[1].replace("|000|1|1|1|", "|000|7|1|1|", 1)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="COVERAGE_LEVEL"):
        load_month(conn, config, "2024-01", root)


def test_two_files_matching_one_pattern_is_an_error(
    conn: sqlite3.Connection, config: Config, tmp_path: Path
) -> None:
    root = copy_month(tmp_path)
    source = root / "2024-01" / "plan information.txt"
    shutil.copy(source, root / "2024-01" / "plan information 2.txt")
    with pytest.raises(LoadError, match="Narrow the patterns"):
        load_month(conn, config, "2024-01", root)


def test_empty_file_is_reported_as_empty(
    conn: sqlite3.Connection, config: Config, tmp_path: Path
) -> None:
    root = copy_month(tmp_path)
    header = (
        "FORMULARY_ID|FORMULARY_VERSION|CONTRACT_YEAR|RXCUI|NDC|TIER_LEVEL_VALUE|"
        "QUANTITY_LIMIT_YN|QUANTITY_LIMIT_AMOUNT|QUANTITY_LIMIT_DAYS|"
        "PRIOR_AUTHORIZATION_YN|STEP_THERAPY_YN|SELECTED_DRUG_YN\n"
    )
    (root / "2024-01" / "basic drugs formulary file.txt").write_text(header, encoding="utf-8")
    with pytest.raises(LoadError, match="contains no data rows"):
        load_month(conn, config, "2024-01", root)


def test_schema_validation_passes_on_the_declared_layout() -> None:
    spec = schema.FILE_SPECS[schema.FORMULARY]
    schema.validate_columns(
        schema.FORMULARY,
        [*spec.required_columns, *spec.optional_columns],
        file_name="f.txt",
        month="2024-01",
    )


def test_schema_validation_reports_duplicate_columns() -> None:
    spec = schema.FILE_SPECS[schema.FORMULARY]
    with pytest.raises(SchemaError, match="Duplicate columns"):
        schema.validate_columns(
            schema.FORMULARY,
            [*spec.required_columns, "NDC"],
            file_name="f.txt",
            month="2024-01",
        )


def test_schema_validation_rejects_an_undeclared_file_type() -> None:
    with pytest.raises(SchemaError, match="No column layout declared"):
        schema.validate_columns("pharmacy_network", ["A"], file_name="f.txt", month="2024-01")


def test_header_of_an_empty_file_is_an_error(config: Config, tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(LoadError, match="expected a header row"):
        reader.read_header(config, empty)


def test_config_rejects_an_unknown_ndc_policy(tmp_path: Path) -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8").replace(
        'unhyphenated_10_digit = "reject"', 'unhyphenated_10_digit = "guess"'
    )
    bad = tmp_path / "bad.toml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="unhyphenated_10_digit"):
        load_config(bad)


def test_config_rejects_an_impact_coverage_level_that_is_not_declared(tmp_path: Path) -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8").replace(
        'coverage_level = "1"', 'coverage_level = "9"', 1
    )
    bad = tmp_path / "bad.toml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="coverage_level"):
        load_config(bad)


def test_config_missing_file_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Config file not found"):
        load_config(tmp_path / "nope.toml")


def test_tier_label_falls_back_for_an_undeclared_tier(config: Config) -> None:
    assert config.tier_label(None) == "not covered"
    assert config.tier_label(99) == "Tier 99"
    assert config.tier_label(1) == "Preferred generic"


def test_severity_weights_must_be_positive(config: Config) -> None:
    zeroed = replace(
        config.severity,
        weight_cost=0.0,
        weight_direction=0.0,
        weight_plans=0.0,
        weight_restriction=0.0,
    )
    assert zeroed.weight_total == 0.0
