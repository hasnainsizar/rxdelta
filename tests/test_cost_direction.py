"""A tier move is labelled by what it does to the member's cost, not its number.

tests/fixtures/cost_direction has one plan over two months:
500011 moves from tier 3 ($47 copay) up to tier 6 ($0 copay), so it costs less;
500012 moves from tier 2 ($10) up to tier 4 ($90), so it costs more;
500013 moves from tier 4 ($90) up to tier 5, 25 percent coinsurance with no
dollar bounds published, so whether it costs more cannot be told.
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
from rxdelta.diff.cost_direction import CostDirection, compare_legs, net_direction, tier_direction
from rxdelta.diff.engine import diff_snapshots
from rxdelta.diff.impact import build_groups
from rxdelta.evals import gold
from rxdelta.ingest.loader import load_month
from rxdelta.report import html
from rxdelta.tools.context import ToolContext
from rxdelta.tools.registry import call_tool
from rxdelta.types import ChangeType, CostLeg, CostRow, PlanKey
from tests.conftest import FIXTURES

COST = FIXTURES / "cost_direction"
EXPECTED = {
    "500011": CostDirection.LOWER,
    "500012": CostDirection.HIGHER,
    "500013": CostDirection.UNKNOWN,
}


def leg(
    cost_type: str,
    amount: float | None,
    low: float | None = None,
    high: float | None = None,
    channel: str = "retail_preferred",
) -> CostLeg:
    return CostLeg(
        channel=channel, cost_type=cost_type, cost_amt=amount, cost_min_amt=low, cost_max_amt=high
    )


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (leg("1", 10.0), leg("1", 47.0), "higher"),
        (leg("1", 47.0), leg("1", 0.0), "lower"),
        (leg("1", 47.0), leg("1", 47.0), "same"),
        (leg("2", 0.25), leg("2", 0.33), "higher"),
        (leg("2", 0.25), leg("1", 0.0), "lower"),
        (leg("1", 0.0), leg("2", 0.25), "higher"),
        (leg("1", 47.0), leg("2", 0.0), "lower"),
        (leg("1", 0.0), leg("2", 0.0), "same"),
        (leg("1", 47.0), leg("2", 0.25), "unknown"),
        (leg("1", 47.0), leg("2", 0.25, 50.0, 150.0), "higher"),
        (leg("1", 200.0), leg("2", 0.25, 50.0, 150.0), "lower"),
        (leg("1", 100.0), leg("2", 0.25, 50.0, 150.0), "unknown"),
        (leg("0", 0.0), leg("1", 10.0), "unknown"),
        (leg("", None), leg("1", 10.0), "unknown"),
    ],
)
def test_compare_legs(config: Config, old: CostLeg, new: CostLeg, expected: str) -> None:
    assert compare_legs(config, old, new) == expected


def _row(days: str, *legs: CostLeg) -> CostRow:
    return CostRow(
        key=PlanKey("H0001", "001", "000"), coverage_level="1", tier=1, days_supply=days, legs=legs
    )


def test_tier_direction_needs_every_comparable_leg_to_agree(config: Config) -> None:
    before = [_row("1", leg("1", 10.0), leg("1", 12.0, channel="mail_preferred"))]
    cheaper = [_row("1", leg("1", 0.0), leg("1", 0.0, channel="mail_preferred"))]
    split = [_row("1", leg("1", 0.0), leg("1", 20.0, channel="mail_preferred"))]
    hidden = [_row("1", leg("0", 0.0), leg("2", 0.25, channel="mail_preferred"))]
    assert tier_direction(config, before, cheaper) == CostDirection.LOWER
    assert tier_direction(config, before, split) == CostDirection.UNKNOWN
    assert tier_direction(config, before, hidden) == CostDirection.UNKNOWN
    # A supply length published at only one tier is not compared.
    assert tier_direction(config, before, [_row("2", leg("1", 0.0))]) == CostDirection.UNKNOWN
    assert tier_direction(config, None, cheaper) == CostDirection.UNKNOWN


def test_net_direction_follows_cost_not_tier_number() -> None:
    up = (ChangeType.TIER_UP,)
    assert net_direction(up, CostDirection.LOWER) == -1
    assert net_direction(up, CostDirection.HIGHER) == 1
    assert net_direction(up, CostDirection.UNKNOWN) == 0
    assert net_direction(up, CostDirection.SAME) == 0
    assert (
        net_direction((ChangeType.TIER_UP, ChangeType.PRIOR_AUTH_ADDED), CostDirection.LOWER) == 0
    )
    assert net_direction((ChangeType.PRIOR_AUTH_ADDED,), None) == 1


@pytest.fixture
def cost_db(tmp_path: Path, config: Config) -> Path:
    db = tmp_path / "cost.db"
    conn = connect(db)
    for month in ("2026-01", "2026-02"):
        load_month(conn, config, month, COST)
    conn.close()
    return db


def test_diff_json_and_csv_carry_the_cost_direction(cost_db: Path, tmp_path: Path) -> None:
    out, csv_out = tmp_path / "d.json", tmp_path / "d.csv"
    result = CliRunner().invoke(
        app,
        [
            "diff",
            "--from",
            "2026-01",
            "--to",
            "2026-02",
            "--db",
            str(cost_db),
            "--json",
            str(out),
            "--csv",
            str(csv_out),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text())
    assert {g["rxcui"]: g["cost_direction"] for g in payload["groups"]} == {
        k: v.value for k, v in EXPECTED.items()
    }
    assert payload["tier_moves_by_cost_direction"] == {
        "tier_up": {"higher": 1, "lower": 1, "same": 0, "unknown": 1}
    }
    assert "member less)" in result.output and "member more)" in result.output
    header, *lines = csv_out.read_text().splitlines()
    assert header.endswith(",cost_direction")
    assert sorted(line.rsplit(",", 1)[1] for line in lines) == ["higher", "lower", "unknown"]


def test_the_report_labels_and_ranks_by_cost(cost_db: Path, config: Config) -> None:
    conn = connect(cost_db)
    groups = build_groups(conn, config, diff_snapshots(conn, "2026-01", "2026-02"))
    rows = {r.rxcui: r for r in html.build_rows(config, groups)}
    assert rows["500011"].change_types == "Tier up (costs the member less)"
    assert rows["500012"].change_types == "Tier up (costs the member more)"
    assert rows["500013"].change_types == "Tier up (cost change not published)"
    ranked = [g.rxcui for g in groups]
    # The tier up that costs more outranks the one that costs less.
    assert ranked.index("500012") < ranked.index("500011")
    by_rxcui = {g.rxcui: g for g in groups}
    assert by_rxcui["500011"].impact.direction == -1
    assert by_rxcui["500012"].impact.direction == 1
    page = html.render(
        config, diff_snapshots(conn, "2026-01", "2026-02"), groups, generated_at="frozen"
    )
    assert "not by the tier number" in page


def test_the_tools_and_gold_agree_on_cost_direction(cost_db: Path, config: Config) -> None:
    ctx = ToolContext(db_path=cost_db, config=config)
    result = call_tool("get_changes", {"from_month": "2026-01", "to_month": "2026-02"}, ctx)
    assert result.ok and result.data is not None
    assert {r.rxcui: r.cost_direction for r in result.data.rows} == EXPECTED

    counted = call_tool(
        "count_changes",
        {"from_month": "2026-01", "to_month": "2026-02", "group_by": "cost_direction"},
        ctx,
    )
    assert counted.data is not None
    assert {g.key: g.counts.drugs for g in counted.data.groups} == {
        "lower": 1,
        "higher": 1,
        "unknown": 1,
    }

    conn = sqlite3.connect(f"file:{cost_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    from_gold = {
        c.rxcui: c.cost_direction for c in gold.changes(conn, "2026-01", "2026-02", config)
    }
    assert from_gold == {k: v.value for k, v in EXPECTED.items()}
