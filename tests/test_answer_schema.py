from __future__ import annotations

import pytest
from pydantic import ValidationError

from rxdelta.ask.answer import (
    AnswerKind,
    ChangeAnswer,
    CostRangeAnswer,
    NoDataAnswer,
    QuestionContext,
    RunnerOutput,
    parse_answer,
)
from rxdelta.ask.values import CoverageState, change_values
from rxdelta.types import ChangeType


def test_every_kind_parses_through_the_discriminated_union() -> None:
    samples = [
        {"kind": "drug_list", "rxcuis": ["1", "2"]},
        {"kind": "count", "count": 3},
        {
            "kind": "change",
            "rxcuis": ["1"],
            "items": [
                {
                    "rxcui": "1",
                    "formulary_id": "F",
                    "change_type": "tier_up",
                    "before": "3",
                    "after": "4",
                }
            ],
        },
        {
            "kind": "coverage",
            "rxcui": "1",
            "month": "2026-01",
            "formulary_id": "F",
            "tier": 2,
            "prior_auth": False,
            "step_therapy": False,
            "quantity_limit": False,
        },
        {"kind": "cost_range", "rxcui": "1", "low": -5.0, "high": 10.0},
        {"kind": "no_data", "reason": "not_on_formulary"},
        {"kind": "refusal", "reason": "medical_advice"},
        {"kind": "clarify", "candidates": [{"product": "Ozempic", "rxcuis": ["1", "2"]}]},
    ]
    kinds = [parse_answer(s).kind for s in samples]
    assert kinds == [k.value for k in AnswerKind]


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "count", "count": -1},
        {"kind": "count", "count": 2, "extra": True},
        {"kind": "no_data", "reason": "because"},
        {"kind": "cost_range", "rxcui": "1", "low": 10.0, "high": 5.0},
        {"kind": "drug_list", "rxcuis": ["abc"]},
        {"kind": "clarify", "candidates": []},
        {"kind": "guess"},
        {
            "kind": "change",
            "rxcuis": ["1"],
            "items": [
                {
                    "rxcui": "1",
                    "formulary_id": "F",
                    "change_type": "pa_added",
                    "before": "no",
                    "after": "yes",
                }
            ],
        },
    ],
)
def test_payloads_are_strict(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        parse_answer(bad)


def test_runner_output_round_trips() -> None:
    output = RunnerOutput(
        answer=NoDataAnswer(reason="unknown_drug"),  # type: ignore[arg-type]
        final_text="No drug by that name.",
        latency_ms=3,
    )
    again = RunnerOutput.model_validate_json(output.model_dump_json())
    assert again == output
    assert isinstance(again.answer, NoDataAnswer)


def test_question_context_carries_no_gold() -> None:
    assert set(QuestionContext.model_fields) == {"default_from", "default_to"}
    with pytest.raises(ValidationError):
        QuestionContext(expected={"kind": "count"})  # type: ignore[call-arg]


def test_change_values_are_canonical() -> None:
    before = CoverageState(3, False, True, True, 30.0, 30)
    after = CoverageState(4, True, False, True, 15.0, 30)
    assert change_values(ChangeType.TIER_UP, before, after) == ("3", "4")
    assert change_values(ChangeType.PRIOR_AUTH_ADDED, before, after) == ("no", "yes")
    assert change_values(ChangeType.STEP_THERAPY_REMOVED, before, after) == ("yes", "no")
    assert change_values(ChangeType.QUANTITY_LIMIT_TIGHTENED, before, after) == (
        "30 per 30 days",
        "15 per 30 days",
    )
    assert change_values(ChangeType.DRUG_ADDED, None, after) == (None, "tier 4")
    assert change_values(ChangeType.DRUG_REMOVED, before, None) == ("tier 3", None)
    none = CoverageState(3, False, False, False, None, None)
    assert change_values(ChangeType.QUANTITY_LIMIT_ADDED, none, before) == (
        "none",
        "30 per 30 days",
    )


def test_models_are_frozen() -> None:
    answer = ChangeAnswer(rxcuis=["1"], items=[])
    with pytest.raises(ValidationError):
        answer.rxcuis = ["2"]
    assert CostRangeAnswer(rxcui="1", low=0.0, high=0.0).currency == "USD"
