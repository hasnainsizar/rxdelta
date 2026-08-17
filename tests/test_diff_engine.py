from __future__ import annotations

import sqlite3

import pytest

from rxdelta.diff.engine import classify, diff_snapshots
from rxdelta.types import ChangeType, DiffError, DrugCoverage, PlanKey

NDC = "11111111101"


def coverage(
    *,
    tier: int = 2,
    prior_auth: bool = False,
    step_therapy: bool = False,
    quantity_limit: bool = False,
    amount: float | None = None,
    days: int | None = None,
) -> DrugCoverage:
    return DrugCoverage(
        ndc_11=NDC,
        rxcui="100001",
        tier_level=tier,
        prior_auth=prior_auth,
        step_therapy=step_therapy,
        quantity_limit=quantity_limit,
        quantity_limit_amount=amount,
        quantity_limit_days=days,
    )


def test_no_change_produces_no_classification() -> None:
    assert classify(coverage(), coverage()) == ()


def test_absent_on_both_sides_produces_no_classification() -> None:
    assert classify(None, None) == ()


def test_drug_added_and_removed() -> None:
    assert classify(None, coverage()) == (ChangeType.DRUG_ADDED,)
    assert classify(coverage(), None) == (ChangeType.DRUG_REMOVED,)


def test_tier_moves() -> None:
    assert classify(coverage(tier=2), coverage(tier=4)) == (ChangeType.TIER_UP,)
    assert classify(coverage(tier=4), coverage(tier=2)) == (ChangeType.TIER_DOWN,)


def test_prior_auth_and_step_therapy() -> None:
    assert classify(coverage(), coverage(prior_auth=True)) == (ChangeType.PRIOR_AUTH_ADDED,)
    assert classify(coverage(prior_auth=True), coverage()) == (ChangeType.PRIOR_AUTH_REMOVED,)
    assert classify(coverage(), coverage(step_therapy=True)) == (ChangeType.STEP_THERAPY_ADDED,)
    assert classify(coverage(step_therapy=True), coverage()) == (ChangeType.STEP_THERAPY_REMOVED,)


def test_quantity_limit_added_and_removed() -> None:
    with_limit = coverage(quantity_limit=True, amount=60, days=30)
    assert classify(coverage(), with_limit) == (ChangeType.QUANTITY_LIMIT_ADDED,)
    assert classify(with_limit, coverage()) == (ChangeType.QUANTITY_LIMIT_REMOVED,)


def test_quantity_limit_tightened_and_loosened() -> None:
    base = coverage(quantity_limit=True, amount=90, days=30)
    tighter = coverage(quantity_limit=True, amount=30, days=30)
    looser = coverage(quantity_limit=True, amount=120, days=30)
    assert classify(base, tighter) == (ChangeType.QUANTITY_LIMIT_TIGHTENED,)
    assert classify(base, looser) == (ChangeType.QUANTITY_LIMIT_LOOSENED,)


def test_quantity_limit_compares_per_day_not_per_window() -> None:
    # 90 per 30 days and 270 per 90 days are the same allowance.
    monthly = coverage(quantity_limit=True, amount=90, days=30)
    quarterly = coverage(quantity_limit=True, amount=270, days=90)
    assert classify(monthly, quarterly) == ()


def test_quantity_limit_with_missing_amounts_is_not_guessed() -> None:
    unknown = coverage(quantity_limit=True, amount=None, days=None)
    known = coverage(quantity_limit=True, amount=60, days=30)
    assert classify(unknown, known) == ()


def test_a_pair_can_carry_several_classifications() -> None:
    before = coverage(tier=2)
    after = coverage(
        tier=5, prior_auth=True, step_therapy=True, quantity_limit=True, amount=30, days=30
    )
    assert classify(before, after) == (
        ChangeType.TIER_UP,
        ChangeType.PRIOR_AUTH_ADDED,
        ChangeType.STEP_THERAPY_ADDED,
        ChangeType.QUANTITY_LIMIT_ADDED,
    )


def test_diff_over_fixtures_finds_every_classification(loaded: sqlite3.Connection) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    found = set(result.counts_by_type())
    assert found == set(ChangeType), f"missing: {sorted(t.value for t in set(ChangeType) - found)}"


def test_diff_fans_changes_out_to_every_plan_on_the_formulary(
    loaded: sqlite3.Connection,
) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    assert result.plans_compared == 2
    per_plan = result.counts_by_plan()
    assert set(per_plan) == {PlanKey("H0001", "001", "000"), PlanKey("H0001", "002", "000")}
    assert len(set(per_plan.values())) == 1


def test_unchanged_drug_is_absent_from_the_diff(loaded: sqlite3.Connection) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    assert "11111111112" not in {c.ndc_11 for c in result.changes}


def test_plan_filter_narrows_to_one_contract(loaded: sqlite3.Connection) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02", plan_filter="H0001")
    assert result.plans_compared == 2
    assert result.plan_filter == "H0001"


def test_unknown_contract_filter_fails_loudly(loaded: sqlite3.Connection) -> None:
    with pytest.raises(DiffError, match="H9999"):
        diff_snapshots(loaded, "2024-01", "2024-02", plan_filter="H9999")


def test_same_month_on_both_sides_fails(loaded: sqlite3.Connection) -> None:
    with pytest.raises(DiffError, match="nothing to compare"):
        diff_snapshots(loaded, "2024-01", "2024-01")


def test_missing_month_names_the_load_command(loaded: sqlite3.Connection) -> None:
    with pytest.raises(DiffError, match="rxdelta load --month 2024-09"):
        diff_snapshots(loaded, "2024-01", "2024-09")


def test_net_direction_and_restriction_flags(loaded: sqlite3.Connection) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    by_ndc = {c.ndc_11: c for c in result.changes}
    tier_up = by_ndc["11111111101"]
    assert tier_up.net_direction == 1
    assert not tier_up.adds_restriction
    multi = by_ndc["11111111113"]
    assert multi.net_direction == 1
    assert multi.adds_restriction
    tier_down = by_ndc["11111111102"]
    assert tier_down.net_direction == -1
