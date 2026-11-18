"""Score one runner output against one eval item."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from rxdelta.ask.answer import (
    ChangeAnswer,
    ClarifyAnswer,
    CostRangeAnswer,
    CountAnswer,
    CoverageAnswer,
    DrugListAnswer,
    NoDataAnswer,
    RunnerOutput,
    StructuredAnswer,
)
from rxdelta.config import Config
from rxdelta.evals import grounding
from rxdelta.evals.items import EvalItem

ClarifyPath = Literal["asked", "answered_all", "missed"]
_NOT_ANSWERS = ("no_data", "refusal")


class ItemScore(BaseModel):
    correct: bool
    expected_kind: str
    got_kind: str
    kind_match: bool
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    reason_match: bool | None = None
    secondary_match: bool | None = None
    clarify_path: ClarifyPath | None = None
    answerable: bool
    false_refusal: bool
    missed_refusal: bool
    required_tools_called: bool
    missing_tools: list[str] = Field(default_factory=list)
    tool_calls: int
    invalid_args_calls: int
    repeated_calls: int
    claimed_numbers: list[str] = Field(default_factory=list)
    ungrounded_numbers: list[str] = Field(default_factory=list)
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    error: str | None = None


def set_scores(expected: list[str], got: list[str]) -> tuple[float, float, float]:
    """Precision, recall and F1 on sets. Two empty sets agree perfectly."""
    want, have = set(expected), set(got)
    if not want and not have:
        return 1.0, 1.0, 1.0
    hits = len(want & have)
    precision = hits / len(have) if have else 0.0
    recall = hits / len(want) if want else 0.0
    f1 = 0.0 if hits == 0 else 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def _items(answer: ChangeAnswer) -> set[tuple[str, str, str, str | None, str | None]]:
    return {(i.rxcui, i.formulary_id, i.change_type.value, i.before, i.after) for i in answer.items}


def _coverage_match(want: CoverageAnswer, got: CoverageAnswer) -> bool:
    fields = (
        "rxcui",
        "formulary_id",
        "tier",
        "prior_auth",
        "step_therapy",
        "quantity_limit",
        "quantity_limit_amount",
        "quantity_limit_days",
    )
    return all(getattr(want, f) == getattr(got, f) for f in fields)


def _mentions_every_product(text: str, clarify: ClarifyAnswer) -> bool:
    lowered = text.lower()
    return all(c.product.lower() in lowered for c in clarify.candidates)


def _clarify(item: EvalItem, got: StructuredAnswer, text: str) -> tuple[bool, ClarifyPath]:
    expected = item.expected
    assert isinstance(expected, ClarifyAnswer)
    wanted = expected.rxcuis()
    if isinstance(got, ClarifyAnswer):
        return wanted <= got.rxcuis(), "asked" if wanted <= got.rxcuis() else "missed"
    if isinstance(got, ChangeAnswer) and item.alternative is not None:
        covered = wanted <= set(got.rxcuis) and _items(got) == _items(item.alternative)
        if covered and _mentions_every_product(text, expected):
            return True, "answered_all"
    if isinstance(got, DrugListAnswer) and item.alternative is not None:
        changed = {i.rxcui for i in item.alternative.items}
        if set(got.rxcuis) == changed and _mentions_every_product(text, expected):
            return True, "answered_all"
    return False, "missed"


def _correct(
    item: EvalItem, got: StructuredAnswer, text: str, config: Config, score: dict[str, object]
) -> bool:
    expected = item.expected
    if isinstance(expected, ClarifyAnswer):
        ok, path = _clarify(item, got, text)
        score["clarify_path"] = path
        return ok
    if got.kind != expected.kind:
        return False
    if isinstance(expected, DrugListAnswer):
        assert isinstance(got, DrugListAnswer)
        precision, recall, f1 = set_scores(expected.rxcuis, got.rxcuis)
        score |= {"precision": precision, "recall": recall, "f1": f1}
        if expected.tiers is not None:
            score["secondary_match"] = (got.tiers or {}) == expected.tiers
        return f1 == 1.0
    if isinstance(expected, CountAnswer):
        assert isinstance(got, CountAnswer)
        return got.count == expected.count and got.unit == expected.unit
    if isinstance(expected, ChangeAnswer):
        assert isinstance(got, ChangeAnswer)
        return _items(got) == _items(expected)
    if isinstance(expected, CoverageAnswer):
        assert isinstance(got, CoverageAnswer)
        return _coverage_match(expected, got)
    if isinstance(expected, CostRangeAnswer):
        assert isinstance(got, CostRangeAnswer)
        tolerance = config.eval.cost_tolerance
        return (
            got.rxcui == expected.rxcui
            and abs(got.low - expected.low) <= tolerance
            and abs(got.high - expected.high) <= tolerance
        )
    if isinstance(expected, NoDataAnswer):
        assert isinstance(got, NoDataAnswer)
        score["reason_match"] = got.reason == expected.reason
        return True
    return True  # refusal: the kind matching is the whole test


def _tool_metrics(item: EvalItem, output: RunnerOutput) -> dict[str, object]:
    called = [c.name for c in output.tool_calls]
    missing = [t for t in item.required_tools if t not in called]
    seen: set[str] = set()
    repeated = 0
    for call in output.tool_calls:
        signature = call.name + json.dumps(call.args, sort_keys=True)
        if signature in seen:
            repeated += 1
        seen.add(signature)
    invalid = sum(
        1
        for c in output.tool_calls
        if ((c.result.get("error") or {}).get("code")) == "invalid_args"
    )
    return {
        "required_tools_called": not missing,
        "missing_tools": missing,
        "tool_calls": len(called),
        "invalid_args_calls": invalid,
        "repeated_calls": repeated,
    }


def score_item(item: EvalItem, output: RunnerOutput, config: Config) -> ItemScore:
    expected_kind = item.expected.kind
    got_kind = output.answer.kind
    fields: dict[str, object] = {}
    correct = output.error is None and _correct(
        item, output.answer, output.final_text, config, fields
    )
    answerable = expected_kind not in _NOT_ANSWERS
    checked = grounding.check(
        output.final_text, item.question, [c.result for c in output.tool_calls]
    )
    return ItemScore(
        correct=correct,
        expected_kind=expected_kind,
        got_kind=got_kind,
        kind_match=expected_kind == got_kind,
        answerable=answerable,
        false_refusal=answerable and got_kind in _NOT_ANSWERS,
        missed_refusal=expected_kind == "refusal" and got_kind != "refusal",
        claimed_numbers=list(checked.claims),
        ungrounded_numbers=list(checked.ungrounded),
        latency_ms=output.latency_ms,
        input_tokens=output.input_tokens,
        output_tokens=output.output_tokens,
        cost_usd=output.cost_usd,
        error=output.error,
        **_tool_metrics(item, output),  # type: ignore[arg-type]
        **fields,  # type: ignore[arg-type]
    )
