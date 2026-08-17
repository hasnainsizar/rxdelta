from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from rxdelta.config import Config, SortKey
from rxdelta.diff.engine import diff_snapshots
from rxdelta.diff.impact import (
    ChangeGroup,
    ImpactRange,
    TierCost,
    build_groups,
    estimate_impact,
    severity_score,
    sort_groups,
    tier_cost,
)
from rxdelta.report import format as fmt
from rxdelta.types import ChangeType, CostLeg, CostRow, PlanKey

PLAN = PlanKey("H0001", "001", "000")


def group(
    ndc: str,
    severity: float,
    plan_count: int,
    low: float,
    high: float,
) -> ChangeGroup:
    return ChangeGroup(
        ndc_11=ndc,
        rxcui="1",
        drug_name=None,
        change_types=(ChangeType.TIER_UP,),
        tier_before=2,
        tier_after=3,
        plans=tuple(PlanKey("H0001", f"{i:03d}", "000") for i in range(plan_count)),
        plan_names=("Plan",),
        impact=ImpactRange(low, high, 1, False, True, ""),
        severity=severity,
    )


def test_ties_break_on_plan_count_then_width_then_midpoint(config: Config) -> None:
    same = [
        group("00000000003", 50.0, 2, 0.0, 10.0),
        group("00000000001", 50.0, 5, 0.0, 10.0),
        group("00000000002", 50.0, 5, 0.0, 40.0),
    ]
    ranked = sort_groups(config, same)
    # plan count first: the two 5-plan groups lead; between them the wider range.
    assert [g.ndc_11 for g in ranked] == [
        "00000000002",
        "00000000001",
        "00000000003",
    ]


def test_severity_still_dominates_every_tiebreaker(config: Config) -> None:
    ranked = sort_groups(
        config,
        [group("00000000001", 10.0, 40, 0.0, 900.0), group("00000000002", 90.0, 1, 0.0, 1.0)],
    )
    assert [g.severity for g in ranked] == [90.0, 10.0]


def test_a_full_tie_falls_back_to_ndc_so_the_order_is_deterministic(config: Config) -> None:
    identical = [group(n, 50.0, 3, 0.0, 10.0) for n in ("00000000003", "00000000001")]
    assert [g.ndc_11 for g in sort_groups(config, identical)] == [
        "00000000001",
        "00000000003",
    ]
    # Same input in the other order gives the same output.
    assert [g.ndc_11 for g in sort_groups(config, list(reversed(identical)))] == [
        "00000000001",
        "00000000003",
    ]


def test_sort_order_comes_from_config_not_the_source(config: Config) -> None:
    reversed_plans = replace(
        config,
        report=replace(
            config.report,
            sort_order=(
                SortKey("severity", True),
                SortKey("plan_count", False),
                SortKey("ndc", False),
            ),
        ),
    )
    ranked = sort_groups(
        reversed_plans,
        [group("00000000001", 50.0, 9, 0.0, 1.0), group("00000000002", 50.0, 2, 0.0, 1.0)],
    )
    assert [g.plan_count for g in ranked] == [2, 9]


def test_severity_no_longer_plateaus_on_large_moves(config: Config) -> None:
    """A hard clamp gave every large move the same score, which cost the ranking
    exactly where it matters, at the top of the table."""
    big = ImpactRange(400.0, 600.0, 1, False, True, "")
    bigger = ImpactRange(900.0, 1100.0, 1, False, True, "")
    assert severity_score(config, bigger, 5, adds_restriction=False) > severity_score(
        config, big, 5, adds_restriction=False
    )


def test_severity_stays_inside_its_scale(config: Config) -> None:
    enormous = ImpactRange(1e6, 1e6, 1, False, True, "")
    nothing = ImpactRange(0.0, 0.0, 0, False, True, "")
    assert severity_score(config, enormous, 10_000, adds_restriction=True) <= 100.0
    assert severity_score(config, nothing, 0, adds_restriction=False) >= 0.0


def test_the_reference_amount_is_the_half_weight_point(config: Config) -> None:
    at_reference = ImpactRange(
        config.severity.cost_reference, config.severity.cost_reference, 1, False, True, ""
    )
    only_cost = replace(
        config,
        severity=replace(
            config.severity,
            weight_direction=0.0,
            weight_plans=0.0,
            weight_restriction=0.0,
            weight_cost=1.0,
        ),
    )
    assert severity_score(only_cost, at_reference, 1, adds_restriction=False) == pytest.approx(
        50.0, abs=0.01
    )


def test_the_ranking_discriminates_on_the_sample_comparison(
    loaded: sqlite3.Connection, config: Config
) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    groups = build_groups(loaded, config, result)
    severities = [g.severity for g in groups]
    assert severities == sorted(severities, reverse=True)


# --- modal case --------------------------------------------------------------


def cost_row(days_supply: str, legs: tuple[CostLeg, ...]) -> CostRow:
    return CostRow(key=PLAN, coverage_level="0", tier=2, days_supply=days_supply, legs=legs)


def leg(channel: str, amount: float) -> CostLeg:
    return CostLeg(
        channel=channel, cost_type="1", cost_amt=amount, cost_min_amt=None, cost_max_amt=None
    )


def test_modal_picks_the_configured_channel_and_supply(config: Config) -> None:
    rows = [
        cost_row("1", (leg("retail_preferred", 10.0), leg("mail_preferred", 4.0))),
        cost_row("2", (leg("retail_preferred", 60.0), leg("mail_preferred", 30.0))),
    ]
    result = tier_cost(config, rows)
    # Modal is the 30 day preferred retail figure, not the cheapest or the mean.
    assert result.modal_low == result.modal_high == 10.0
    assert (result.low, result.high) == (4.0, 20.0)


def test_modal_is_absent_when_the_plan_does_not_publish_that_combination(
    config: Config,
) -> None:
    rows = [cost_row("2", (leg("mail_preferred", 30.0),))]
    result = tier_cost(config, rows)
    assert not result.modal_known
    assert result.known


def test_a_coinsurance_modal_stays_a_range_rather_than_becoming_a_point(
    config: Config,
) -> None:
    coins = CostLeg(
        channel="retail_preferred",
        cost_type="2",
        cost_amt=33.0,
        cost_min_amt=100.0,
        cost_max_amt=500.0,
    )
    result = tier_cost(config, [cost_row("1", (coins,))])
    assert (result.modal_low, result.modal_high) == (100.0, 500.0)


def test_modal_delta_is_carried_through_the_estimate(config: Config) -> None:
    before = TierCost(9.0, 12.0, 4, 0, 10.0, 10.0)
    after = TierCost(81.0, 108.0, 4, 0, 90.0, 90.0)
    impact = estimate_impact(before, after, 1, open_ended=False)
    assert (impact.modal_low, impact.modal_high) == (80.0, 80.0)
    assert (impact.low, impact.high) == (69.0, 99.0)


def test_modal_is_none_when_either_side_lacks_it(config: Config) -> None:
    before = TierCost(9.0, 12.0, 4, 0, None, None)
    after = TierCost(81.0, 108.0, 4, 0, 90.0, 90.0)
    assert not estimate_impact(before, after, 1, open_ended=False).modal_known


def test_a_modal_direction_can_disagree_with_a_range_that_spans_zero() -> None:
    """The case the flag exists for: the modal fill clearly rises, but the bound
    across all channels and supply lengths still admits a saving."""
    impact = ImpactRange(-18.09, 1056.49, 1, False, True, "", modal_low=22.0, modal_high=44.0)
    assert impact.spans_zero
    assert impact.modal_direction == 1
    assert fmt.modal_mark(impact) == ("↑", "increase", "up")
    assert fmt.modal_figure(impact) == "+$22.00 to +$44.00"


def test_an_unpublished_modal_is_marked_not_silently_substituted() -> None:
    impact = ImpactRange(-18.09, 1056.49, 1, False, True, "")
    assert fmt.modal_figure(impact) == "not published"
    assert fmt.modal_mark(impact)[1] == "modal case not published"


def test_modal_figure_collapses_to_one_number_for_a_copay_tier() -> None:
    impact = ImpactRange(69.0, 99.0, 1, False, True, "", modal_low=80.0, modal_high=80.0)
    assert fmt.modal_figure(impact) == "+$80.00"
    assert impact.modal_direction == 1


def test_spans_zero_only_when_the_bound_really_crosses() -> None:
    assert ImpactRange(-1.0, 1.0, 1, False, True, "").spans_zero
    assert not ImpactRange(0.0, 1.0, 1, False, True, "").spans_zero
    assert not ImpactRange(-2.0, -1.0, -1, False, True, "").spans_zero
    assert not ImpactRange(-1.0, 1.0, 1, False, False, "").spans_zero
