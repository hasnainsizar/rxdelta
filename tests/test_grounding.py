from __future__ import annotations

from typing import Any

import pytest

from rxdelta.evals.grounding import check, claimed_numbers

TOOL: list[dict[str, Any]] = [
    {
        "data": {
            "display_name": "insulin lispro protamine, human 75 UNT/ML / insulin lispro 25 UNT/ML "
            "Injectable Suspension [Humalog Mix 75/25]",
            "tier": 4,
            "change": {"low": -5.0, "high": 12.5},
            "total": 1234.5,
            "coinsurance": 0.25,
            "before_value": "30 per 30 days",
            "formulary_id": "00026123",
            "plans": 3,
        },
        "note": "Showing 10 of 18.",
    }
]


@pytest.mark.parametrize(
    ("text", "claims", "ungrounded"),
    [
        ("Humalog Mix 75/25 moved to tier 4.", ["4"], []),
        ("Two match:\n1. One on tier 4\n2. Another\n  3) A third", ["4"], []),
        ("Step 1. It moved to tier 9.", ["1", "9"], ["1", "9"]),
        ("Humalog 75/25 is now tier 4", ["4"], []),
        ("The change is -$5.00 to $12.50 a month.", ["-$5.00", "$12.50"], []),
        ("It now costs $5 less.", ["$5"], []),
        ("That is 5 dollars cheaper.", ["5"], []),
        ("costs $1,234.50 a year", ["$1,234.50"], []),
        ("costs 1,234.5", ["1,234.5"], []),
        ("costs $12.5 and then $12.50", ["$12.5", "$12.50"], []),
        ("about 25% coinsurance", ["25%"], []),
        ("between 40 to 60 dollars", ["40", "60"], ["40", "60"]),
        ("between 10 to 20 dollars", ["10", "20"], ["20"]),
        ("from $5-$12.50 a month", ["$5", "$12.50"], []),
        ("a range of 3-4", ["3", "4"], []),
        ("a range of 3-7", ["3", "7"], ["7"]),
        ("Ozempic 1 mg is tier 7", ["7"], ["7"]),
        ("limit is 30 per 30 days", ["30", "30"], []),
        ("Showing 10 of 18 rows; 3 plans", ["10", "18", "3"], []),
        ("tier 6", ["6"], ["6"]),
        ("costs -$9.99", ["-$9.99"], ["-$9.99"]),
        ("about 4321 dollars", ["4321"], ["4321"]),
    ],
)
def test_claims_and_grounding(text: str, claims: list[str], ungrounded: list[str]) -> None:
    result = check(text, "", TOOL)
    assert list(result.claims) == claims
    assert list(result.ungrounded) == ungrounded


def test_months_years_and_identifiers_are_not_claims() -> None:
    text = "In March 2026 (2026-03), NDC 88000-0141-01 on plan H1234-001 moved."
    assert claimed_numbers(text) == []


def test_numbers_in_the_question_are_not_claims() -> None:
    question = "Since 12 drugs moved down a tier on formulary 00026124, which were they?"
    assert (
        claimed_numbers("You said 12 drugs; formulary 00026124 shows two.", question=question) == []
    )
    assert claimed_numbers("You said 13 drugs.", question=question) == ["13"]


def test_numbers_inside_returned_names_are_not_claims() -> None:
    names = ["Prairie Part D Saver 2 (PDP)"]
    assert claimed_numbers("Prairie Part D Saver 2 (PDP) changed tier 3", names=names) == ["3"]


def test_a_formulary_id_counts_as_a_claim_and_is_normalized() -> None:
    assert check("formulary 00026123", "", TOOL).grounded
    assert not check("formulary 00026999", "", TOOL).grounded


def test_unicode_minus_and_dashes() -> None:
    assert list(check("change −$5.00 – $12.50", "", TOOL).ungrounded) == []


def test_empty_text_has_no_claims() -> None:
    result = check("", "anything 42", [])
    assert result.claims == () and result.grounded
