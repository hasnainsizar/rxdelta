from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from rxdelta.cli import app
from tests.conftest import CONFIG_PATH, SNAPSHOTS

runner = CliRunner()


def load_both(db: Path) -> None:
    for month in ("2024-01", "2024-02"):
        result = runner.invoke(
            app,
            [
                "load",
                "--month",
                month,
                "--dir",
                str(SNAPSHOTS),
                "--db",
                str(db),
                "--config",
                str(CONFIG_PATH),
            ],
        )
        assert result.exit_code == 0, result.output


def test_status_on_an_empty_database_says_so(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "--db", str(tmp_path / "e.db")])
    assert result.exit_code == 0
    assert "No months loaded" in result.output


def test_load_then_status_reports_the_ingest_log(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    load_both(db)
    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0
    assert "2024-01" in result.output
    assert "Ingest log" in result.output
    assert "No rejected rows" in result.output


def test_summary_prints_a_rollup_and_the_limitations(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    load_both(db)
    result = runner.invoke(
        app, ["summary", "--from", "2024-01", "--to", "2024-02", "--db", str(db)]
    )
    assert result.exit_code == 0
    assert "By change type" in result.output
    assert "By plan" in result.output
    assert "What these estimates do not account for" in result.output


def test_diff_writes_json_and_csv(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    load_both(db)
    json_path = tmp_path / "out.json"
    csv_path = tmp_path / "out.csv"
    result = runner.invoke(
        app,
        [
            "diff",
            "--from",
            "2024-01",
            "--to",
            "2024-02",
            "--db",
            str(db),
            "--json",
            str(json_path),
            "--csv",
            str(csv_path),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(json_path.read_text())
    assert payload["month_from"] == "2024-01"
    assert payload["groups"]
    assert payload["limitations"]
    assert csv_path.read_text().startswith("ndc_11,rxcui,change_types")


def test_report_writes_html_and_warns_about_the_low_count(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    load_both(db)
    out = tmp_path / "report.html"
    result = runner.invoke(
        app, ["report", "--from", "2024-01", "--to", "2024-02", "--out", str(out), "--db", str(db)]
    )
    assert result.exit_code == 0
    assert out.is_file()
    assert "below the reporting floor" in result.output


def test_a_missing_month_exits_non_zero_with_a_readable_message(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    load_both(db)
    result = runner.invoke(app, ["diff", "--from", "2024-01", "--to", "2030-01", "--db", str(db)])
    assert result.exit_code == 1


def test_version_prints_something(tmp_path: Path) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_severity_distribution_reports_spread(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    load_both(db)
    out = tmp_path / "d.json"
    result = runner.invoke(
        app,
        [
            "summary",
            "--from",
            "2024-01",
            "--to",
            "2024-02",
            "--db",
            str(db),
            "--severity-distribution",
            "--json",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Severity distribution" in result.output
    assert "distinct severity value(s)" in result.output
    assert "Largest tie" in result.output
    payload = json.loads(out.read_text())["severity_distribution"]
    assert payload["groups"] > 0
    assert payload["distinct"] >= 1
    assert sum(payload["buckets"].values()) == payload["groups"]


def test_frozen_timestamp_makes_the_report_reproducible(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    load_both(db)
    first, second = tmp_path / "a.html", tmp_path / "b.html"
    for out in (first, second):
        result = runner.invoke(
            app,
            [
                "report",
                "--from",
                "2024-01",
                "--to",
                "2024-02",
                "--out",
                str(out),
                "--db",
                str(db),
                "--frozen-timestamp",
            ],
        )
        assert result.exit_code == 0, result.output
    assert first.read_text() == second.read_text()
    assert "2024-01 to 2024-02 comparison" in first.read_text()


def test_without_the_flag_the_report_carries_a_wall_clock_stamp(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    load_both(db)
    out = tmp_path / "a.html"
    runner.invoke(
        app, ["report", "--from", "2024-01", "--to", "2024-02", "--out", str(out), "--db", str(db)]
    )
    assert "UTC" in out.read_text()


def test_names_refresh_needs_a_loaded_snapshot(tmp_path: Path) -> None:
    result = runner.invoke(app, ["names", "refresh", "--db", str(tmp_path / "empty.db")])
    assert result.exit_code == 0
    assert "No RXCUIs found" in result.output


def test_names_refresh_does_no_work_when_the_cache_already_covers_everything(
    tmp_path: Path,
) -> None:
    """The offline guarantee: with a complete cache, refresh touches no network."""
    db = tmp_path / "t.db"
    load_both(db)
    cache = tmp_path / "names.csv"
    conn = sqlite3.connect(db)
    rxcuis = [str(r[0]) for r in conn.execute("SELECT DISTINCT rxcui FROM formulary").fetchall()]
    conn.close()
    cache.write_text(
        "rxcui,name,source,fetched_at\n" + "".join(f"{r},Drug {r},fixture,t\n" for r in rxcuis),
        encoding="utf-8",
    )
    config = tmp_path / "cfg.toml"
    config.write_text(
        CONFIG_PATH.read_text(encoding="utf-8").replace(
            'cache_path = "data/reference/drug_names.csv"', f'cache_path = "{cache}"'
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["names", "refresh", "--db", str(db), "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "Nothing to fetch" in result.output
