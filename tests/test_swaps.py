"""Package code swaps, one rule for the diff, the report, the tools and gold.

tests/fixtures/swaps has one formulary and one plan over two months:
500001 swaps NDC and nothing else, 500002 swaps NDC and moves from tier 2 to 4,
500003 leaves the formulary, and 500004 does not change at all.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rxdelta.cli import app
from rxdelta.config import Config
from rxdelta.db import connect
from rxdelta.diff.engine import diff_snapshots
from rxdelta.diff.impact import build_groups
from rxdelta.evals import gold
from rxdelta.ingest.loader import load_month
from rxdelta.report import html
from rxdelta.swaps import pair_swaps
from rxdelta.tools.context import ToolContext
from rxdelta.tools.registry import call_tool
from rxdelta.types import ChangeType
from tests.conftest import FIXTURES

SWAPS = FIXTURES / "swaps"


@pytest.fixture
def swap_db(tmp_path: Path, config: Config) -> Path:
    db = tmp_path / "swaps.db"
    conn = connect(db)
    for month in ("2026-01", "2026-02"):
        load_month(conn, config, month, SWAPS)
    conn.close()
    return db


def test_pair_swaps_splits_swaps_from_real_changes() -> None:
    removed = [("A", "old"), ("B", "old"), ("C", "gone")]
    added = [("A", "new"), ("D", "new")]
    result = pair_swaps(
        removed,
        added,
        removed_key=lambda r: r[0],
        added_key=lambda r: r[0],
        listed_after=lambda key: key in {"A", "B", "D"},
        listed_before=lambda key: key in {"A", "B", "C"},
    )
    assert result.pairs == [(("A", "old"), ("A", "new"))]
    # B lost an NDC but is still listed under another one, so it is not removed.
    assert result.removals == [("C", "gone")]
    assert result.additions == [("D", "new")]


def test_pair_swaps_pairs_every_combination_for_one_drug() -> None:
    result = pair_swaps(
        ["a1", "a2"],
        ["b1"],
        removed_key=lambda _r: "X",
        added_key=lambda _a: "X",
        listed_after=lambda _k: True,
        listed_before=lambda _k: True,
    )
    assert result.pairs == [("a1", "b1"), ("a2", "b1")]
    assert result.removals == [] and result.additions == []


def test_the_diff_pairs_swaps(swap_db: Path) -> None:
    conn = sqlite3.connect(swap_db)
    conn.row_factory = sqlite3.Row
    result = diff_snapshots(conn, "2026-01", "2026-02")
    found = {
        (
            c.rxcui,
            t,
            c.before.ndc_11 if c.before else None,
            c.after.ndc_11 if c.after else None,
        )
        for c in result.changes
        for t in c.change_types
    }
    # A swap alone is no change; a swap with a tier move is one tier change.
    assert found == {
        ("500002", ChangeType.TIER_UP, "55555000201", "55555000202"),
        ("500003", ChangeType.DRUG_REMOVED, "55555000301", None),
    }


def test_diff_json_and_report_show_the_same_two_changes(
    swap_db: Path, config: Config, tmp_path: Path
) -> None:
    out = tmp_path / "diff.json"
    result = CliRunner().invoke(
        app,
        ["diff", "--from", "2026-01", "--to", "2026-02", "--db", str(swap_db), "--json", str(out)],
    )
    assert result.exit_code == 0, result.output
    groups = json.loads(out.read_text())["groups"]
    assert sorted((g["rxcui"], g["change_types"]) for g in groups) == [
        ("500002", ["tier_up"]),
        ("500003", ["drug_removed"]),
    ]
    assert json.loads(out.read_text())["counts_by_change_type"] == {
        "drug_removed": 1,
        "tier_up": 1,
    }

    conn = connect(swap_db)
    diff = diff_snapshots(conn, "2026-01", "2026-02")
    rows = html.build_rows(config, build_groups(conn, config, diff))
    assert sorted((r.rxcui, r.change_types) for r in rows) == [
        ("500002", "Tier up (costs the member more)"),
        ("500003", "Drug removed"),
    ]
    page = html.render(config, diff, build_groups(conn, config, diff), generated_at="frozen")
    assert "55555-0001-02" not in page  # the plain swap never reaches the report


def test_the_tools_and_gold_agree_with_the_diff(swap_db: Path, config: Config) -> None:
    ctx = ToolContext(db_path=swap_db, config=config)
    result = call_tool("get_changes", {"from_month": "2026-01", "to_month": "2026-02"}, ctx)
    assert result.ok and result.data is not None
    from_tool = {(r.rxcui, r.change_type, tuple(r.ndcs)) for r in result.data.rows}
    assert from_tool == {
        ("500002", ChangeType.TIER_UP, ("55555000201", "55555000202")),
        ("500003", ChangeType.DRUG_REMOVED, ("55555000301",)),
    }
    tier = next(r for r in result.data.rows if r.rxcui == "500002")
    assert (tier.before_value, tier.after_value) == ("2", "4")

    conn = sqlite3.connect(f"file:{swap_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    from_gold = {
        (c.rxcui, c.change_type, c.before, c.after)
        for c in gold.changes(conn, "2026-01", "2026-02")
    }
    assert from_gold == {
        ("500002", ChangeType.TIER_UP, "2", "4"),
        ("500003", ChangeType.DRUG_REMOVED, "tier 3", None),
    }
