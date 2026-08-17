from __future__ import annotations

import sqlite3

from rxdelta.config import Config
from rxdelta.diff.engine import diff_snapshots
from rxdelta.diff.impact import (
    UNPRICED,
    ChangeGroup,
    ImpactRange,
    TierCost,
    build_groups,
    estimate_impact,
    severity_score,
    tier_cost,
)
from rxdelta.types import ChangeType, CostLeg, CostRow, PlanKey

PLAN = PlanKey("H0001", "001", "000")


def cost_row(days_supply: str, legs: tuple[CostLeg, ...], tier: int = 2) -> CostRow:
    return CostRow(key=PLAN, coverage_level="0", tier=tier, days_supply=days_supply, legs=legs)


def copay(amount: float) -> CostLeg:
    return CostLeg(
        channel="retail_preferred",
        cost_type="1",
        cost_amt=amount,
        cost_min_amt=None,
        cost_max_amt=None,
    )


def coinsurance(low: float, high: float) -> CostLeg:
    return CostLeg(
        channel="retail_preferred",
        cost_type="2",
        cost_amt=25.0,
        cost_min_amt=low,
        cost_max_amt=high,
    )


def test_tier_cost_spans_the_channels(config: Config) -> None:
    row = cost_row("1", (copay(10.0), copay(12.0), copay(9.0), copay(11.0)))
    result = tier_cost(config, [row])
    assert result.known
    assert (result.low, result.high) == (9.0, 12.0)
    assert result.observations == 4


def test_tier_cost_normalizes_supply_length(config: Config) -> None:
    # Code 2 is a 90 day supply, so $30 there is $10 per 30 days.
    monthly = tier_cost(config, [cost_row("1", (copay(10.0),))])
    quarterly = tier_cost(config, [cost_row("2", (copay(30.0),))])
    assert monthly.low == quarterly.low == 10.0


def test_coinsurance_uses_the_published_dollar_bounds_not_the_percentage(
    config: Config,
) -> None:
    result = tier_cost(config, [cost_row("1", (coinsurance(100.0, 500.0),))])
    assert (result.low, result.high) == (100.0, 500.0)


def test_coinsurance_without_bounds_is_counted_as_unpriced(config: Config) -> None:
    leg = CostLeg(
        channel="retail_preferred",
        cost_type="2",
        cost_amt=25.0,
        cost_min_amt=None,
        cost_max_amt=None,
    )
    result = tier_cost(config, [cost_row("1", (leg,))])
    assert not result.known
    assert result.unpriced_legs == 1


def test_tier_cost_with_no_rows_is_unknown(config: Config) -> None:
    assert not tier_cost(config, None).known
    assert not tier_cost(config, []).known


def test_estimate_is_a_range_with_low_below_high() -> None:
    before = TierCost(low=9.0, high=12.0, observations=4, unpriced_legs=0)
    after = TierCost(low=81.0, high=108.0, observations=4, unpriced_legs=0)
    impact = estimate_impact(before, after, 1, open_ended=False)
    assert impact.priced
    assert impact.low < impact.high
    assert (impact.low, impact.high) == (69.0, 99.0)


def test_a_downward_move_produces_a_negative_range() -> None:
    before = TierCost(low=81.0, high=108.0, observations=4, unpriced_legs=0)
    after = TierCost(low=9.0, high=12.0, observations=4, unpriced_legs=0)
    impact = estimate_impact(before, after, -1, open_ended=False)
    assert impact.high < 0
    assert impact.low <= impact.high


def test_open_ended_change_prices_only_the_covered_side() -> None:
    before = TierCost(low=81.0, high=108.0, observations=4, unpriced_legs=0)
    impact = estimate_impact(before, TierCost(None, None, 0, 0), 1, open_ended=True)
    assert impact.open_ended
    assert (impact.low, impact.high) == (81.0, 108.0)
    assert "not published" in impact.basis


def test_unpriced_when_neither_side_has_cost_rows() -> None:
    unknown = TierCost(None, None, 0, 0)
    assert estimate_impact(unknown, unknown, 1, open_ended=False) == UNPRICED
    assert estimate_impact(unknown, unknown, 1, open_ended=True) == UNPRICED


def test_severity_rises_with_cost_reach_and_restriction(config: Config) -> None:
    small = ImpactRange(1.0, 2.0, 1, False, True, "")
    large = ImpactRange(200.0, 400.0, 1, False, True, "")
    assert severity_score(config, large, 1, adds_restriction=False) > severity_score(
        config, small, 1, adds_restriction=False
    )
    assert severity_score(config, small, 40, adds_restriction=False) > severity_score(
        config, small, 1, adds_restriction=False
    )
    assert severity_score(config, small, 1, adds_restriction=True) > severity_score(
        config, small, 1, adds_restriction=False
    )


def test_severity_stays_within_zero_and_one_hundred(config: Config) -> None:
    huge = ImpactRange(10_000.0, 20_000.0, 1, False, True, "")
    nothing = ImpactRange(0.0, 0.0, -1, False, True, "")
    assert severity_score(config, huge, 10_000, adds_restriction=True) <= 100.0
    assert severity_score(config, nothing, 0, adds_restriction=False) >= 0.0


def test_a_cost_increase_outranks_the_same_size_decrease(config: Config) -> None:
    up = ImpactRange(50.0, 60.0, 1, False, True, "")
    down = ImpactRange(-60.0, -50.0, -1, False, True, "")
    assert severity_score(config, up, 5, adds_restriction=False) > severity_score(
        config, down, 5, adds_restriction=False
    )


def groups_by_ndc(groups: list[ChangeGroup]) -> dict[str, ChangeGroup]:
    return {g.ndc_11: g for g in groups}


def test_build_groups_prices_every_group_as_a_range(
    loaded: sqlite3.Connection, config: Config
) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    groups = build_groups(loaded, config, result)
    assert groups
    for group in groups:
        assert group.impact.low <= group.impact.high
        assert 0.0 <= group.severity <= 100.0


def test_build_groups_rolls_plans_up_and_sorts_by_severity(
    loaded: sqlite3.Connection, config: Config
) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    groups = build_groups(loaded, config, result)
    assert all(g.plan_count == 2 for g in groups)
    assert [g.severity for g in groups] == sorted((g.severity for g in groups), reverse=True)


def test_tier_move_impact_matches_the_fixture_cost_table(
    loaded: sqlite3.Connection, config: Config
) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    group = groups_by_ndc(build_groups(loaded, config, result))["11111111101"]
    # Tier 2 costs 9.00 to 12.00, tier 4 costs 81.00 to 99.00 in the fixture.
    assert group.change_types == (ChangeType.TIER_UP,)
    assert (group.impact.low, group.impact.high) == (69.0, 99.0)
    assert group.impact.direction == 1


def test_drug_removal_is_marked_open_ended(loaded: sqlite3.Connection, config: Config) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    group = groups_by_ndc(build_groups(loaded, config, result))["11111111111"]
    assert group.change_types == (ChangeType.DRUG_REMOVED,)
    assert group.impact.open_ended
    assert group.impact.low <= group.impact.high
    assert group.tier_after is None
